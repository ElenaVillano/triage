from triage.component.catwalk.evaluation import (
    SORT_TRIALS,
    ModelEvaluator,
    generate_binary_at_x,
    query_subset_table,
    subset_labels_and_predictions,
)
from triage.component.catwalk.metrics import Metric
import testing.postgresql
import datetime
import re

import factory
import numpy
from numpy.testing import assert_almost_equal, assert_array_equal
import pandas
from sqlalchemy.sql.expression import text
from triage.component.catwalk.utils import filename_friendly_hash, get_subset_table_name
from tests.utils import fake_labels, fake_trained_model, MockMatrixStore
from tests.results_tests.factories import (
    ModelFactory,
    EvaluationFactory,
    PredictionFactory,
    SubsetFactory,
    session,
)


@Metric(greater_is_better=True)
def always_half(predictions_proba, predictions_binary, labels, parameters):
    return 0.5


SUBSETS = [
    {
        "name": "evens",
        "query": """
            select distinct entity_id
            from events
            where entity_id % 2 = 0
            and outcome_date < '{as_of_date}'::date
        """,
    },
    {
        "name": "odds",
        "query": """
            select distinct entity_id
            from events
            where entity_id % 2 = 1
            and outcome_date < '{as_of_date}'::date
        """,
    },
    {
        "name": "empty",
        "query": """
            select distinct entity_id
            from events
            where entity_id = -1
            and outcome_date < '{as_of_date}'::date
        """,
    },
]

TRAIN_END_TIME = datetime.datetime(2016, 1, 1)


def populate_subset_data(db_engine, subset, entity_ids, as_of_date=TRAIN_END_TIME):
    table_name = get_subset_table_name(subset)
    query_where_clause = re.search("where.*[0-9]", subset["query"]).group()

    db_engine.execute(
        f"""
        create table {table_name} (
            entity_id int,
            as_of_date date,
            active bool
        )
        """
    )

    for entity_id in entity_ids:
        insert_query = f"""
            with unfiltered_row as (
                select {entity_id} as entity_id,
                       '{as_of_date}'::date as as_of_date,
                       true as active
            )
            insert into {table_name}
            select entity_id, as_of_date, active
            from unfiltered_row
            {query_where_clause}
            """
        db_engine.execute(text(insert_query).execution_options(autocommit=True))


def test_all_same_labels(db_engine_with_results_schema):
    num_entities = 5
    trained_model, model_id = fake_trained_model(
        db_engine_with_results_schema,
        train_end_time=TRAIN_END_TIME,
    )

    for label_value in [0, 1]:
        labels = [label_value] * num_entities

        # We should be able to calculate accuracy even if all of the labels
        # are the same, but ROC_AUC requires some positive and some
        # negative labels, so we should get one NULL value
        # for this config
        training_metric_groups = [{"metrics": ["accuracy", "roc_auc"]}]
        
        # Acquire fake data and objects to be used in the tests
        model_evaluator = ModelEvaluator(
            {},
            training_metric_groups,
            db_engine_with_results_schema,
        )
        fake_matrix_store = MockMatrixStore(
            matrix_type="train",
            matrix_uuid=str(labels),
            label_count=num_entities,
            db_engine=db_engine_with_results_schema,
            init_labels=pandas.DataFrame(
                {
                    "label_value": labels,
                    "entity_id": list(range(num_entities)),
                    "as_of_date": [TRAIN_END_TIME] * num_entities,
                }
            ).set_index(["entity_id", "as_of_date"]).label_value,
            init_as_of_dates=[TRAIN_END_TIME],
        )
        
            
        model_evaluator.evaluate(
            trained_model.predict_proba(labels)[:, 1], fake_matrix_store, model_id
        )
        
        for metric, best, worst, stochastic in db_engine_with_results_schema.execute(
            f"""select metric, best_value, worst_value, stochastic_value
            from train_results.evaluations
            where model_id = %s and
            evaluation_start_time = %s
            order by 1""",
            (
                model_id,
                fake_matrix_store.as_of_dates[0]
            ),
        ):
            if metric == "accuracy":
                assert best is not None
                assert worst is not None
                assert stochastic is not None
            else:
                assert best is None
                assert worst is None
                assert stochastic is None


def test_subset_labels_and_predictions(db_engine_with_results_schema):
    num_entities = 5
    labels = [0, 1, 0, 1, 0]
    predictions_proba = numpy.array([0.6, 0.4, 0.55, 0.70, 0.3])

    fake_matrix_store = MockMatrixStore(
        matrix_type="test",
        matrix_uuid="abcde",
        label_count=num_entities,
        db_engine=db_engine_with_results_schema,
        init_labels=pandas.DataFrame(
            {
                "label_value": labels,
                "entity_id": list(range(num_entities)),
                "as_of_date": [TRAIN_END_TIME] * num_entities,
            }
        ).set_index(["entity_id", "as_of_date"]).label_value,
        init_as_of_dates=[TRAIN_END_TIME],
    )

    for subset in SUBSETS:
        if subset["name"] == "evens":
            expected_result = 3
        elif subset["name"] == "odds":
            expected_result = 2
        elif subset["name"] == "empty":
            expected_result = 0
        
        populate_subset_data(db_engine_with_results_schema, subset, list(range(num_entities)))
        subset_labels, subset_predictions = subset_labels_and_predictions(
                subset_df=query_subset_table(
                    db_engine_with_results_schema,
                    fake_matrix_store.as_of_dates,
                    get_subset_table_name(subset),
                ),
                predictions_proba=predictions_proba,
                labels=fake_matrix_store.labels,
        )

        assert len(subset_labels) == expected_result
        assert len(subset_predictions) == expected_result


def test_evaluating_early_warning(db_engine_with_results_schema):
    num_entities = 10
    labels = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]

    # Set up testing configuration parameters
    testing_metric_groups = [
        {
            "metrics": [
                "precision@",
                "recall@",
                "true positives@",
                "true negatives@",
                "false positives@",
                "false negatives@",
            ],
            "thresholds": {"percentiles": [5.0, 10.0], "top_n": [5, 10]},
        },
        {
            "metrics": [
                "f1",
                "mediocre",
                "accuracy",
                "roc_auc",
                "average precision score",
            ]
        },
        {"metrics": ["fbeta@"], "parameters": [{"beta": 0.75}, {"beta": 1.25}]},
    ]

    training_metric_groups = [{"metrics": ["accuracy", "roc_auc"]}]

    custom_metrics = {"mediocre": always_half}

    # Acquire fake data and objects to be used in the tests
    model_evaluator = ModelEvaluator(
        testing_metric_groups,
        training_metric_groups,
        db_engine_with_results_schema,
        custom_metrics=custom_metrics,
    )

    fake_test_matrix_store = MockMatrixStore(
        matrix_type="test",
        matrix_uuid="efgh",
        label_count=num_entities,
        db_engine=db_engine_with_results_schema,
        init_labels=pandas.DataFrame(
            {
                "label_value": labels,
                "entity_id": list(range(num_entities)),
                "as_of_date": [TRAIN_END_TIME] * num_entities,
            }
        ).set_index(["entity_id", "as_of_date"]).label_value,
        init_as_of_dates=[TRAIN_END_TIME],
    )
    fake_train_matrix_store = MockMatrixStore(
        matrix_type="train",
        matrix_uuid="1234",
        label_count=num_entities,
        db_engine=db_engine_with_results_schema,
        init_labels=pandas.DataFrame(
            {
                "label_value": labels,
                "entity_id": list(range(num_entities)),
                "as_of_date": [TRAIN_END_TIME] * num_entities,
            }
        ).set_index(["entity_id", "as_of_date"]).label_value,
        init_as_of_dates=[TRAIN_END_TIME],
    )

    trained_model, model_id = fake_trained_model(
        db_engine_with_results_schema,
        train_end_time=TRAIN_END_TIME,
    )

    # ensure that the matrix uuid is present
    matrix_uuids = [
        row[0]
        for row in db_engine_with_results_schema.execute("select matrix_uuid from test_results.evaluations")
    ]
    assert all(matrix_uuid == "efgh" for matrix_uuid in matrix_uuids)

    # Evaluate the training metrics and test
    model_evaluator.evaluate(
        trained_model.predict_proba(labels)[:, 1], fake_train_matrix_store, model_id
    )
    records = [
        row[0]
        for row in db_engine_with_results_schema.execute(
            """select distinct(metric || parameter)
            from train_results.evaluations
            where model_id = %s and
            evaluation_start_time = %s
            order by 1""",
            (model_id, fake_train_matrix_store.as_of_dates[0]),
        )
    ]
    assert records == ["accuracy", "roc_auc"]

    # Run tests for overall and subset evaluations
    for subset in [None] + SUBSETS:
        if subset is None:
            where_hash = ""
        else:
            populate_subset_data(db_engine_with_results_schema, subset, list(range(num_entities)))
            SubsetFactory(subset_hash=filename_friendly_hash(subset))
            session.commit()
            where_hash = f"and subset_hash = '{filename_friendly_hash(subset)}'"

        # Evaluate the testing metrics and test for all of them.
        model_evaluator.evaluate(
            trained_model.predict_proba(labels)[:, 1],
            fake_test_matrix_store,
            model_id,
            subset,
        )

        records = [
            row[0]
            for row in db_engine_with_results_schema.execute(
                f"""\
                select distinct(metric || parameter)
                from test_results.evaluations
                where model_id = %s and
                evaluation_start_time = %s
                {where_hash}
                order by 1
                """,
                (
                    model_id,
                    fake_test_matrix_store.as_of_dates[0]
                ),
            )
        ]
        assert records == [
            "accuracy",
            "average precision score",
            "f1",
            "false negatives@10.0_pct",
            "false negatives@10_abs",
            "false negatives@5.0_pct",
            "false negatives@5_abs",
            "false positives@10.0_pct",
            "false positives@10_abs",
            "false positives@5.0_pct",
            "false positives@5_abs",
            "fbeta@0.75_beta",
            "fbeta@1.25_beta",
            "mediocre",
            "precision@10.0_pct",
            "precision@10_abs",
            "precision@5.0_pct",
            "precision@5_abs",
            "recall@10.0_pct",
            "recall@10_abs",
            "recall@5.0_pct",
            "recall@5_abs",
            "roc_auc",
            "true negatives@10.0_pct",
            "true negatives@10_abs",
            "true negatives@5.0_pct",
            "true negatives@5_abs",
            "true positives@10.0_pct",
            "true positives@10_abs",
            "true positives@5.0_pct",
            "true positives@5_abs",
        ]

        # Evaluate the training metrics and test
        model_evaluator.evaluate(
            trained_model.predict_proba(labels)[:, 1],
            fake_train_matrix_store,
            model_id,
            subset,
        )
        
        records = [
            row[0]
            for row in db_engine_with_results_schema.execute(
                f"""select distinct(metric || parameter)
                from train_results.evaluations
                where model_id = %s and
                evaluation_start_time = %s
                {where_hash}
                order by 1""",
                (
                    model_id,
                    fake_train_matrix_store.as_of_dates[0]
                ),
            )
        ]
        assert records == ["accuracy", "roc_auc"]

    # ensure that the matrix uuid is present
    matrix_uuids = [
        row[0]
        for row in db_engine_with_results_schema.execute("select matrix_uuid from train_results.evaluations")
    ]
    assert all(matrix_uuid == "1234" for matrix_uuid in matrix_uuids)


def test_model_scoring_inspections(db_engine_with_results_schema):
    testing_metric_groups = [
        {
            "metrics": ["precision@", "recall@", "fpr@"],
            "thresholds": {"percentiles": [50.0], "top_n": [3]},
        },
        {
            # ensure we test a non-thresholded metric as well
            "metrics": ["accuracy"]
        },
    ]
    training_metric_groups = [
        {"metrics": ["accuracy"], "thresholds": {"percentiles": [50.0]}}
    ]

    model_evaluator = ModelEvaluator(
        testing_metric_groups,
        training_metric_groups,
        db_engine_with_results_schema,
    )

    testing_labels = numpy.array([1, 0, numpy.nan, 1, 0])
    testing_prediction_probas = numpy.array([0.56, 0.4, 0.55, 0.5, 0.3])

    training_labels = numpy.array(
        [0, 0, 1, 1, 1, 0, 1, 1]
    )
    training_prediction_probas = numpy.array(
        [0.6, 0.4, 0.55, 0.70, 0.3, 0.2, 0.8, 0.6]
    )

    fake_train_matrix_store = MockMatrixStore(
        "train", "efgh", 5, db_engine_with_results_schema, training_labels
    )
    fake_test_matrix_store = MockMatrixStore(
        "test", "1234", 5, db_engine_with_results_schema, testing_labels
    )

    trained_model, model_id = fake_trained_model(
        db_engine_with_results_schema,
        train_end_time=TRAIN_END_TIME,
    )

    # Evaluate testing matrix and test the results
    model_evaluator.evaluate(
        testing_prediction_probas, fake_test_matrix_store, model_id
    )
    for record in db_engine_with_results_schema.execute(
        """select * from test_results.evaluations
        where model_id = %s and evaluation_start_time = %s
        order by 1""",
        (model_id, fake_test_matrix_store.as_of_dates[0]),
    ):
        assert record["num_labeled_examples"] == 4
        assert record["num_positive_labels"] == 2
        if record["parameter"] == "":
            assert record["num_labeled_above_threshold"] == 4
        elif "pct" in record["parameter"]:
            assert record["num_labeled_above_threshold"] == 1
        else:
            assert record["num_labeled_above_threshold"] == 2

    # Evaluate the training matrix and test the results
    model_evaluator.evaluate(
        training_prediction_probas, fake_train_matrix_store, model_id
    )
    for record in db_engine_with_results_schema.execute(
        """select * from train_results.evaluations
        where model_id = %s and evaluation_start_time = %s
        order by 1""",
        (model_id, fake_train_matrix_store.as_of_dates[0]),
    ):
        assert record["num_labeled_examples"] == 8
        assert record["num_positive_labels"] == 5
        assert record["worst_value"] == 0.625
        assert record["best_value"] == 0.625
        assert record["stochastic_value"] == 0.625
        assert record["num_sort_trials"] == 1 # best/worst are same, should shortcut trials
        assert record["standard_deviation"] == None


def test_evaluation_with_sort_ties(db_engine_with_results_schema):
    model_evaluator = ModelEvaluator(
        testing_metric_groups=[
            {
                "metrics": ["precision@"],
                "thresholds": {"top_n": [3]},
            },
        ],
        training_metric_groups=[],
        db_engine=db_engine_with_results_schema,
    )
    testing_labels = numpy.array([1, 0, 1, 0, 0])
    testing_prediction_probas = numpy.array([0.56, 0.55, 0.5, 0.5, 0.3])

    fake_test_matrix_store = MockMatrixStore(
        "test", "1234", 5, db_engine_with_results_schema, testing_labels
    )

    trained_model, model_id = fake_trained_model(
        db_engine_with_results_schema,
        train_end_time=TRAIN_END_TIME,
    )
    model_evaluator.evaluate(
        testing_prediction_probas, fake_test_matrix_store, model_id
    )
    for record in db_engine_with_results_schema.execute(
        """select * from test_results.evaluations
        where model_id = %s and evaluation_start_time = %s
        order by 1""",
        (model_id, fake_test_matrix_store.as_of_dates[0]),
    ):
        assert record["num_labeled_examples"] == 5
        assert record["num_positive_labels"] == 2
        assert_almost_equal(float(record["worst_value"]), 0.33333, 5)
        assert_almost_equal(float(record["best_value"]), 0.66666, 5)
        assert record["num_sort_trials"] == SORT_TRIALS
        assert record["stochastic_value"] > record["worst_value"]
        assert record["stochastic_value"] < record["best_value"]
        assert record["standard_deviation"]


def test_ModelEvaluator_needs_evaluation(db_engine_with_results_schema):
    # TEST SETUP:

    # create two models: one that has zero evaluations,
    # one that has an evaluation for precision@100_abs
    # both overall and for each subset
    model_with_evaluations = ModelFactory()
    model_without_evaluations = ModelFactory()

    eval_time = datetime.datetime(2016, 1, 1)
    as_of_date_frequency = "3d"
    for subset_hash in [""] + [filename_friendly_hash(subset) for subset in SUBSETS]:
        EvaluationFactory(
            model_rel=model_with_evaluations,
            evaluation_start_time=eval_time,
            evaluation_end_time=eval_time,
            as_of_date_frequency=as_of_date_frequency,
            metric="precision@",
            parameter="100_abs",
            subset_hash=subset_hash,
        )
    session.commit()

    # make a test matrix to pass in
    metadata_overrides = {
        'as_of_date_frequency': as_of_date_frequency,
        'as_of_times': [eval_time],
    }
    test_matrix_store = MockMatrixStore(
        "test", "1234", 5, db_engine_with_results_schema, metadata_overrides=metadata_overrides
    )
    train_matrix_store = MockMatrixStore(
        "train", "2345", 5, db_engine_with_results_schema, metadata_overrides=metadata_overrides
    )

    # the evaluated model has test evaluations for precision, but not recall,
    # so this needs evaluations
    for subset in [None] + SUBSETS:
        if not subset:
            subset_hash = ""
        else:
            subset_hash = filename_friendly_hash(subset)
        
        assert ModelEvaluator(
            testing_metric_groups=[{
                "metrics": ["precision@", "recall@"],
                "thresholds": {"top_n": [100]},
            }],
            training_metric_groups=[],
            db_engine=db_engine_with_results_schema,
        ).needs_evaluations(
            matrix_store=test_matrix_store,
            model_id=model_with_evaluations.model_id,
            subset_hash=subset_hash,
        )

    # the evaluated model has test evaluations for precision,
    # so this should not need evaluations
    for subset in [None] + SUBSETS:
        if not subset:
            subset_hash = ""
        else:
            subset_hash = filename_friendly_hash(subset)

        assert not ModelEvaluator(
            testing_metric_groups=[{
                "metrics": ["precision@"],
                "thresholds": {"top_n": [100]},
            }],
            training_metric_groups=[],
            db_engine=db_engine_with_results_schema,
        ).needs_evaluations(
            matrix_store=test_matrix_store,
            model_id=model_with_evaluations.model_id,
            subset_hash=subset_hash,
        )

    # the non-evaluated model has no evaluations,
    # so this should need evaluations
    for subset in [None] + SUBSETS:
        if not subset:
            subset_hash = ""
        else:
            subset_hash = filename_friendly_hash(subset)
        
        assert ModelEvaluator(
            testing_metric_groups=[{
                "metrics": ["precision@"],
                "thresholds": {"top_n": [100]},
            }],
            training_metric_groups=[],
            db_engine=db_engine_with_results_schema,
        ).needs_evaluations(
            matrix_store=test_matrix_store,
            model_id=model_without_evaluations.model_id,
            subset_hash=subset_hash,
        )

    # the evaluated model has no *train* evaluations,
    # so the train matrix should need evaluations
    for subset in [None] + SUBSETS:
        if not subset:
            subset_hash = ""
        else:
            subset_hash = filename_friendly_hash(subset)
        
        assert ModelEvaluator(
            testing_metric_groups=[{
                "metrics": ["precision@"],
                "thresholds": {"top_n": [100]},
            }],
            training_metric_groups=[{
                "metrics": ["precision@"],
                "thresholds": {"top_n": [100]},
            }],
            db_engine=db_engine_with_results_schema,
        ).needs_evaluations(
            matrix_store=train_matrix_store,
            model_id=model_with_evaluations.model_id,
            subset_hash=subset_hash,
        )
    session.close()
    session.remove()


def test_generate_binary_at_x():
    input_array = numpy.array([0.9, 0.8, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.6])

    # bug can arise when the same value spans both sides of threshold
    assert_array_equal(
        generate_binary_at_x(input_array, 50, "percentile"),
        numpy.array([1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
    )

    assert_array_equal(
        generate_binary_at_x(input_array, 2),
        numpy.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
    )

    assert_array_equal(
        generate_binary_at_x(numpy.array([]), 2),
        numpy.array([])
    )
