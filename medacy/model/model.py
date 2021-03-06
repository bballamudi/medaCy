import importlib
import logging
import os
import time
from shutil import copyfile
from statistics import mean

import joblib
from pathos.multiprocessing import ProcessingPool as Pool, cpu_count
from sklearn_crfsuite import metrics
from tabulate import tabulate

from medacy.data.dataset import Dataset
from medacy.model._model import construct_annotations_from_tuples, predict_document, create_folds
from medacy.pipelines.base.base_pipeline import BasePipeline


class Model:
    """
    A medaCy Model allows the fitting of a named entity recognition model to a given dataset according to the
    configuration of a given medaCy pipeline. Once fitted, Model instances can be used to predict over documents.
    Also included is a function for cross validating over a dataset for measuring the performance of a pipeline.

    :ivar pipeline: a medaCy pipeline, must be a subclass of BasePipeline (see medacy.pipelines.base.BasePipeline)
    :ivar model: weights, if the model has been fitted
    :ivar n_jobs: the number of CPU cores to be used by processes of this instance, defaults to the number of CPUs on
    the machine it's running on
    :ivar X_data: X_data from the pipeline; primarily for internal use
    :ivar y_data: y_data from the pipeline; primarily for internal use
    """

    def __init__(self, medacy_pipeline, model=None, n_jobs=cpu_count()):

        if not isinstance(medacy_pipeline, BasePipeline):
            raise TypeError("Pipeline must be a medaCy pipeline that interfaces medacy.pipelines.base.BasePipeline")

        self.pipeline = medacy_pipeline
        self.model = model

        # These arrays will store the sequences of features and sequences of corresponding labels
        self.X_data = []
        self.y_data = []
        self.n_jobs = n_jobs

        # Run an initializing document through the pipeline to register all token extensions.
        # This allows the gathering of pipeline information prior to fitting with live data.
        doc = self.pipeline(medacy_pipeline.spacy_pipeline.make_doc("Initialize"), predict=True)
        if doc is None:
            raise IOError("Model could not be initialized with the set pipeline.")

    def preprocess(self, dataset, asynchronous=False):
        """
        Preprocess dataset into a list of sequences and tags.

        :param dataset: Dataset object to preprocess.
        :param asynchronous: Boolean for whether the preprocessing should be done asynchronously.
        """
        if asynchronous:
            logging.info('Preprocessing data asynchronously...')
            self.X_data = []
            self.y_data = []
            pool = Pool(nodes=self.n_jobs)

            results = [pool.apipe(self._extract_features, data_file) for data_file in dataset]

            while any([i.ready() is False for i in results]):
                time.sleep(1)

            for i in results:
                X, y = i.get()
                self.X_data += X
                self.y_data += y

        else:
            logging.info('Preprocessing data synchronously...')
            self.X_data = []
            self.y_data = []
            # Run all Docs through the pipeline before extracting features, allowing for pipeline components
            # that require inter-dependent doc objects
            docs = [self._run_through_pipeline(data_file) for data_file in dataset]
            for doc in docs:
                features, labels = self._extract_features(doc)
                self.X_data += features
                self.y_data += labels

    def fit(self, dataset, asynchronous=False):
        """
        Runs dataset through the designated pipeline, extracts features, and fits a conditional random field.

        :param dataset: Instance of Dataset.
        :param asynchronous: Boolean for whether the preprocessing should be done asynchronously.
        :return model: a trained instance of a sklearn_crfsuite.CRF model.
        """

        if not isinstance(dataset, Dataset):
            raise TypeError("Must pass in an instance of Dataset containing your training files")
        if not isinstance(self.pipeline, BasePipeline):
            raise TypeError("Model object must contain a medacy pipeline to pre-process data")

        self.preprocess(dataset, asynchronous)

        logging.info("Currently Waiting")

        learner_name, learner = self.pipeline.get_learner()
        logging.info("Training: %s", learner_name)

        assert self.X_data, "Training data is empty."

        train_data = [x[0] for x in self.X_data]
        learner.fit(train_data, self.y_data)
        logging.info("Successfully Trained: %s", learner_name)

        self.model = learner
        return self.model

    def predict(self, input_data, prediction_directory=None):
        """
        Generates predictions over a string or a input_data utilizing the pipeline equipped to the instance.

        :param input_data: a string, Dataset, or directory path to predict over
        :param prediction_directory: The directory to write predictions if doing bulk prediction
            (default: */prediction* sub-directory of Dataset)
        :return: if input_data is a str, returns an Annotations of the predictions;
            if input_data is a Dataset or a valid directory path, returns a Dataset of the predictions.

        Note that if input_data is supposed to be a directory path but the directory is not found, it will be predicted
        over as a string. This can be prevented by validating inputs with os.path.isdir().
        """

        if self.model is None:
            raise RuntimeError("Must fit or load a pickled model before predicting")

        if isinstance(input_data, str) and not os.path.isdir(input_data):
            doc = self.pipeline.spacy_pipeline.make_doc(input_data)
            doc.set_extension('file_name', default=None, force=True)
            doc._.file_name = 'STRING_INPUT'
            doc = self.pipeline(doc, predict=True)
            annotations = predict_document(self.model, doc, self.pipeline)
            return annotations

        if isinstance(input_data, Dataset):
            input_files = [d.txt_path for d in input_data]
            # Change input_data to point to the Dataset's directory path so that we can use it
            # to create the prediction directory
            input_data = input_data.data_directory
        elif os.path.isdir(input_data):
            input_files = [os.path.join(input_data, f) for f in os.listdir(input_data) if f.endswith('.txt')]
        else:
            raise ValueError(f"'input_data' must be a string (which can be a directory path) or a Dataset, but is {repr(input_data)}")

        if prediction_directory is None:
            prediction_directory = os.path.join(input_data, 'predictions')
            if os.path.isdir(prediction_directory):
                logging.warning("Overwriting existing predictions at %s", prediction_directory)
            else:
                os.mkdir(prediction_directory)

        for file_path in input_files:
            file_name = os.path.basename(file_path).strip('.txt')
            logging.info("Predicting file: %s", file_path)

            with open(file_path, 'r') as f:
                doc = self.pipeline.spacy_pipeline.make_doc(f.read())

            doc.set_extension('file_name', default=None, force=True)
            doc._.file_name = file_name

            # run through the pipeline
            doc = self.pipeline(doc, predict=True)

            # Predict, creating a new Annotations object
            annotations = predict_document(self.model, doc, self.pipeline)
            logging.debug("Writing to: %s", os.path.join(prediction_directory, file_name + ".ann"))
            annotations.to_ann(write_location=os.path.join(prediction_directory, file_name + ".ann"))

            # Copy the txt file so that the output will also be a Dataset
            copyfile(file_path, os.path.join(prediction_directory, file_name + ".txt"))

        return Dataset(prediction_directory)

    def cross_validate(self, training_dataset=None, num_folds=5, prediction_directory=None, groundtruth_directory=None, asynchronous=False):
        """
        Performs k-fold stratified cross-validation using our model and pipeline.

        If the training dataset, groundtruth_directory and prediction_directory are passed, intermediate predictions during cross validation
        are written to the directory `write_predictions`. This allows one to construct a confusion matrix or to compute
        the prediction ambiguity with the methods present in the Dataset class to support pipeline development without
        a designated evaluation set.

        :param training_dataset: Dataset that is being cross validated (optional)
        :param num_folds: number of folds to split training data into for cross validation
        :param prediction_directory: directory to write predictions of cross validation to or `True` for default predictions sub-directory.
        :param groundtruth_directory: directory to write the ground truth MedaCy evaluates on
        :param asynchronous: Boolean for whether the preprocessing should be done asynchronously.
        :return: Prints out performance metrics, if prediction_directory
        """

        if num_folds <= 1:
            raise ValueError("Number of folds for cross validation must be greater than 1, but is %s" % repr(num_folds))

        if prediction_directory is not None and training_dataset is None:
            raise ValueError("Cannot generate predictions during cross validation if training dataset is not given."
                             " Please pass the training dataset in the 'training_dataset' parameter.")
        if groundtruth_directory is not None and training_dataset is None:
            raise ValueError("Cannot generate groundtruth during cross validation if training dataset is not given."
                             " Please pass the training dataset in the 'training_dataset' parameter.")

        pipeline_report = self.pipeline.get_report()

        self.preprocess(training_dataset, asynchronous)

        if not (self.X_data and self.y_data):
            raise RuntimeError("Must have features and labels extracted for cross validation")

        tags = sorted(training_dataset.get_labels(as_list=True))
        self.pipeline.entities = tags
        logging.info('Tagset: %s', tags)

        eval_stats = {}

        # Dict for storing mapping of sequences to their corresponding file
        groundtruth_by_document = {filename: [] for filename in {x[2] for x in self.X_data}}
        preds_by_document = {filename: [] for filename in {x[2] for x in self.X_data}}

        folds = create_folds(self.y_data, num_folds)

        for fold_num, fold_data in enumerate(folds, 1):
            train_indices, test_indices = fold_data
            fold_statistics = {}
            learner_name, learner = self.pipeline.get_learner()

            X_train = [self.X_data[index] for index in train_indices]
            y_train = [self.y_data[index] for index in train_indices]

            X_test = [self.X_data[index] for index in test_indices]
            y_test = [self.y_data[index] for index in test_indices]

            logging.info("Training Fold %i", fold_num)
            train_data = [x[0] for x in X_train]
            test_data = [x[0] for x in X_test]
            learner.fit(train_data, y_train)
            y_pred = learner.predict(test_data)

            if groundtruth_directory is not None:
                # Flattening nested structures into 2d lists
                document_indices = []
                span_indices = []
                for sequence in X_test:
                    document_indices += [sequence[2]] * len(sequence[0])
                    span_indices += list(sequence[1])
                groundtruth = [element for sentence in y_test for element in sentence]

                # Map the predicted sequences to their corresponding documents
                i = 0

                while i < len(groundtruth):
                    if groundtruth[i] == 'O':
                        i += 1
                        continue

                    entity = groundtruth[i]
                    document = document_indices[i]
                    first_start, first_end = span_indices[i]
                    # Ensure that consecutive tokens with the same label are merged
                    while i < len(groundtruth) - 1 and groundtruth[i + 1] == entity:  # If inside entity, keep incrementing
                        i += 1

                    last_start, last_end = span_indices[i]
                    groundtruth_by_document[document].append((entity, first_start, last_end))
                    i += 1

            if prediction_directory is not None:
                # Flattening nested structures into 2d lists
                document_indices = []
                span_indices = []

                for sequence in X_test:
                    document_indices += [sequence[2]] * len(sequence[0])
                    span_indices += list(sequence[1])

                predictions = [element for sentence in y_pred for element in sentence]

                # Map the predicted sequences to their corresponding documents
                i = 0

                while i < len(predictions):
                    if predictions[i] == 'O':
                        i += 1
                        continue

                    entity = predictions[i]
                    document = document_indices[i]
                    first_start, first_end = span_indices[i]

                    # Ensure that consecutive tokens with the same label are merged
                    while i < len(predictions) - 1 and predictions[i + 1] == entity:  # If inside entity, keep incrementing
                        i += 1

                    last_start, last_end = span_indices[i]
                    preds_by_document[document].append((entity, first_start, last_end))
                    i += 1

            # Write the metrics for this fold.
            for label in tags:
                fold_statistics[label] = {
                    "recall": metrics.flat_recall_score(y_test, y_pred, average='weighted', labels=[label]),
                    "precision": metrics.flat_precision_score(y_test, y_pred, average='weighted', labels=[label]),
                    "f1": metrics.flat_f1_score(y_test, y_pred, average='weighted', labels=[label])
                }

            # add averages
            fold_statistics['system'] = {
                "recall": metrics.flat_recall_score(y_test, y_pred, average='weighted', labels=tags),
                "precision": metrics.flat_precision_score(y_test, y_pred, average='weighted', labels=tags),
                "f1": metrics.flat_f1_score(y_test, y_pred, average='weighted', labels=tags)
            }

            table_data = [
                [label,
                 format(fold_statistics[label]['precision'], ".3f"),
                 format(fold_statistics[label]['recall'], ".3f"),
                 format(fold_statistics[label]['f1'], ".3f")
                 ] for label in tags + ['system']
            ]

            logging.info('\n' + tabulate(table_data, headers=['Entity', 'Precision', 'Recall', 'F1'], tablefmt='orgtbl'))

            eval_stats[fold_num] = fold_statistics

        statistics_all_folds = {}

        for label in tags + ['system']:
            statistics_all_folds[label] = {
                'precision_average': mean(eval_stats[fold][label]['precision'] for fold in eval_stats),
                'precision_max': max(eval_stats[fold][label]['precision'] for fold in eval_stats),
                'precision_min': min(eval_stats[fold][label]['precision'] for fold in eval_stats),
                'recall_average': mean(eval_stats[fold][label]['recall'] for fold in eval_stats),
                'recall_max': max(eval_stats[fold][label]['recall'] for fold in eval_stats),
                'f1_average': mean(eval_stats[fold][label]['f1'] for fold in eval_stats),
                'f1_max': max(eval_stats[fold][label]['f1'] for fold in eval_stats),
                'f1_min': min(eval_stats[fold][label]['f1'] for fold in eval_stats),
            }

        entity_counts = training_dataset.compute_counts()

        table_data = [
            [f"{label} ({entity_counts[label]})",  # Entity (Count)
             format(statistics_all_folds[label]['precision_average'], ".3f"),
             format(statistics_all_folds[label]['recall_average'], ".3f"),
             format(statistics_all_folds[label]['f1_average'], ".3f"),
             format(statistics_all_folds[label]['f1_min'], ".3f"),
             format(statistics_all_folds[label]['f1_max'], ".3f")
             ] for label in tags + ['system']
        ]

        # Combine the pipeline report and the resulting data, then log it or print it (whichever ensures that it prints)

        output_str = '\n' + pipeline_report + '\n\n' + tabulate(
            table_data,
            headers=['Entity (Count)', 'Precision', 'Recall', 'F1', 'F1_Min', 'F1_Max'],
            tablefmt='orgtbl'
        )

        if logging.root.level > logging.INFO:
            print(output_str)
        else:
            logging.info(output_str)

        if prediction_directory:
            
            prediction_directory = os.path.join(training_dataset.data_directory, "predictions")
            groundtruth_directory = os.path.join(training_dataset.data_directory, "groundtruth")
            
            # Write annotations generated from cross-validation
            self.create_annotation_directory(
                directory=prediction_directory,
                training_dataset=training_dataset,
                option="predictions"
            )

            # Write medaCy ground truth generated from cross-validation
            self.create_annotation_directory(
                directory=groundtruth_directory,
                training_dataset=training_dataset,
                option="groundtruth"
            )
            
            # Add predicted/known annotations to the folders containing groundtruth and predictions respectively
            self.predict_annotation_evaluation(
                directory=groundtruth_directory,
                training_dataset=training_dataset,
                preds_by_document=preds_by_document,
                groundtruth_by_document=groundtruth_by_document,
                option="groundtruth"
            )

            self.predict_annotation_evaluation(
                directory=prediction_directory,
                training_dataset=training_dataset,
                preds_by_document=preds_by_document,
                groundtruth_by_document=groundtruth_by_document,
                option="predictions"
            )

            return Dataset(prediction_directory)
        else:
            return statistics_all_folds

    def create_annotation_directory(self, directory, training_dataset, option):
        if isinstance(directory, str):
            directory = directory
        else:
            directory = os.path.join(training_dataset.data_directory, option)
        if os.path.isdir(directory):
            logging.warning("Overwriting existing %s",option)
        else:
            os.makedirs(directory)
        return directory

    def predict_annotation_evaluation(self, directory, training_dataset, preds_by_document, groundtruth_by_document, option):
        for data_file in training_dataset:
            logging.info("Predicting %s file: %s", option, data_file.file_name)
            with open(data_file.txt_path, 'r') as f:
                doc = self.pipeline.spacy_pipeline.make_doc(f.read())
                
            if option == "groundtruth":
                preds = groundtruth_by_document[data_file.file_name]
            else:
                preds = preds_by_document[data_file.file_name]

            annotations = construct_annotations_from_tuples(doc, preds)
            annotations.to_ann(write_location=os.path.join(directory, data_file.file_name + ".ann"))
        
        return Dataset(directory)

    def _run_through_pipeline(self, data_file):
        """
        Runs a DataFile through the pipeline, returning the resulting Doc object
        :param data_file: instance of DataFile
        :return: a Doc object
        """
        nlp = self.pipeline.spacy_pipeline
        logging.info("Processing file: %s", data_file.file_name)

        with open(data_file.txt_path, 'r', encoding='utf-8') as f:
            doc = nlp.make_doc(f.read())

        # Link ann_path to doc
        doc.set_extension('gold_annotation_file', default=None, force=True)
        doc.set_extension('file_name', default=None, force=True)

        doc._.gold_annotation_file = data_file.ann_path
        doc._.file_name = data_file.txt_path

        # run 'er through
        return self.pipeline(doc)

    def _extract_features(self, doc):
        """
        Extracts features from a Doc
        :param doc: an instance of Doc
        :return: a tuple of the feature dict and label list
        """

        feature_extractor = self.pipeline.get_feature_extractor()
        features, labels = feature_extractor(doc)

        logging.info("%s: Feature Extraction Completed (num_sequences=%i)" % (doc._.file_name, len(labels)))
        return features, labels

    def load(self, path):
        """
        Loads a pickled model.

        :param path: File path to directory where fitted model should be dumped
        :return:
        """
        model_name, model = self.pipeline.get_learner()

        if model_name == 'BiLSTM+CRF' or model_name == 'BERT':
            model.load(path)
            self.model = model
        else:
            self.model = joblib.load(path)

    def dump(self, path):
        """
        Dumps a model into a pickle file

        :param path: Directory path to dump the model
        :return:
        """
        if self.model is None:
            raise RuntimeError("Must fit model before dumping.")

        model_name, _ = self.pipeline.get_learner()

        if model_name == 'BiLSTM+CRF' or model_name == 'BERT':
            self.model.save(path)
        else:
            joblib.dump(self.model, path)

    @staticmethod
    def load_external(package_name):
        """
        Loads an external medaCy compatible Model. Require's the models package to be installed
        Alternatively, you can import the package directly and call it's .load() method.

        :param package_name: the package name of the model
        :return: an instance of Model that is configured and loaded - ready for prediction.
        """
        if importlib.util.find_spec(package_name) is None:
            raise ImportError("Package not installed: %s" % package_name)
        return importlib.import_module(package_name).load()
