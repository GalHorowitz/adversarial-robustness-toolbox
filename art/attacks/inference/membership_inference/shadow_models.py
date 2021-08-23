# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2020
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
This module implements membership inference attacks.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import math
from functools import reduce
from typing import Callable, Tuple, TYPE_CHECKING, Union, Sequence

import numpy as np

if TYPE_CHECKING:
    from art.utils import CLASSIFIER_TYPE
    from art.estimators.classification.scikitlearn import ScikitlearnClassifier
    from art.estimators.classification import PyTorchClassifier, TensorFlowV2Classifier


class ShadowModels:
    """
    Utility for training shadow models and generating shadow-datasets for membership inference attacks.
    """

    def __init__(
        self,
        shadow_model_template: Union["ScikitlearnClassifier", "PyTorchClassifier", "TensorFlowV2Classifier"],
        num_shadow_models: int = 3,
        random_state=None,
    ):
        """
        Initializes shadow models using the provided template.

        :param shadow_model_template: Untrained classifier model to be used as a template for shadow models. Should be
                                      as similar as possible to the target model.
        :param num_shadow_models: How many shadow models to train to generate the shadow dataset.
        :param random_state: Seed for the numpy default random number generator.
        """

        self._shadow_models = [shadow_model_template.clone_for_refitting() for _ in range(num_shadow_models)]
        self._shadow_models_train_sets = [None] * num_shadow_models
        self._input_shape = shadow_model_template.input_shape
        self._rng = np.random.default_rng(seed=random_state)

    def generate_shadow_dataset(
        self,
        x: np.ndarray,
        y: np.ndarray,
        member_ratio: float = 0.5,
    ) -> Tuple[Tuple[np.ndarray, np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """
        Generates a shadow dataset (member and nonmember samples and their corresponding model predictions) by splitting
        the dataset into training and testing samples, and then training the shadow models on the result.

        :param x: The samples used to train the shadow models.
        :param y: True labels for the dataset samples.
        :param member_ratio: Percentage of the data that should be used to train the shadow models. Must be between 0
                             and 1.

        :return: The shadow dataset generated. The shape is `((member_samples, true_label, model_prediction),
                 (nonmember_samples, true_label, model_prediction))`.
        """

        if len(x) != len(y):
            raise ValueError("Number of samples in dataset does not match number of labels")

        # Shuffle data set
        random_indices = np.random.permutation(len(x))
        x, y = x[random_indices].astype(np.float32), y[random_indices].astype(np.float32)

        shadow_dataset_size = len(x) // len(self._shadow_models)

        member_samples = []
        member_true_label = []
        member_prediction = []
        nonmember_samples = []
        nonmember_true_label = []
        nonmember_prediction = []

        # Train and create predictions for every model
        for i, shadow_model in enumerate(self._shadow_models):
            shadow_x = x[shadow_dataset_size * i : shadow_dataset_size * (i + 1)]
            shadow_y = y[shadow_dataset_size * i : shadow_dataset_size * (i + 1)]

            shadow_x_train = shadow_x[: int(member_ratio * shadow_dataset_size)]
            shadow_y_train = shadow_y[: int(member_ratio * shadow_dataset_size)]
            shadow_x_test = shadow_x[int(member_ratio * shadow_dataset_size) :]
            shadow_y_test = shadow_y[int(member_ratio * shadow_dataset_size) :]

            self._shadow_models_train_sets[i] = (shadow_x_train, shadow_y_train)

            shadow_model.fit(shadow_x_train, shadow_y_train)

            member_samples.append(shadow_x_train)
            member_true_label.append(shadow_y_train)
            member_prediction.append(shadow_model.predict(shadow_x_train))

            nonmember_samples.append(shadow_x_test)
            nonmember_true_label.append(shadow_y_test)
            nonmember_prediction.append(shadow_model.predict(shadow_x_test))

        def concat(first: np.ndarray, second: np.ndarray) -> np.ndarray:
            return np.concatenate((first, second))

        # Concatenate the results of all the shadow models
        all_member_samples = reduce(concat, member_samples)
        all_member_true_label = reduce(concat, member_true_label)
        all_member_prediction = reduce(concat, member_prediction)
        all_nonmember_samples = reduce(concat, nonmember_samples)
        all_nonmember_true_label = reduce(concat, nonmember_true_label)
        all_nonmember_prediction = reduce(concat, nonmember_prediction)

        return (
            (all_member_samples, all_member_true_label, all_member_prediction),
            (all_nonmember_samples, all_nonmember_true_label, all_nonmember_prediction),
        )

    def _default_random_record(self) -> np.ndarray:
        return self._rng.random(self._input_shape)

    def _default_randomize_features(self, record: np.ndarray, num_features: int) -> np.ndarray:
        new_record = record.copy()
        for _ in range(num_features):
            new_record[self._rng.integers(0, self._input_shape)] = self._rng.random()
        return new_record

    def _hill_climbing_synthesis(
        self,
        target_classifier: "CLASSIFIER_TYPE",
        wanted_class: int,
        min_confidence: float,
        max_features_randomized: int,
        max_iterations: int = 40,
        max_rejections: int = 3,
        min_features_randomized: int = 1,
        random_record_fn: Callable[[], np.ndarray] = None,
        randomize_features_fn: Callable[[np.ndarray, int], np.ndarray] = None,
    ) -> np.ndarray:
        """
        This method implements the hill climbing algorithm from R. Shokri et al. (2017)

        Paper Link: https://arxiv.org/abs/1610.05820

        :param target_classifier: The classifier to synthesize data from.
        :param wanted_class: The class the synthesized record will have.
        :param min_confidence: The minimum confidence the classifier assigns the target class for the record to be
                               accepted (i.e. the hill-climbing algorithm is finished).
        :param max_features_randomized: The initial amount of features to randomize in each climbing step. A good
                                        default value is one half of the number of features.
        :param max_iterations: The maximum amount of iterations to try and improve the classifier's confidence in the
                               generated record. This is essentially the maximum number of hill-climbing steps.
        :param max_rejections: The maximum amount of rejections (i.e. a step which did not improve the confidence)
                               before starting to fine-tune the record (i.e. making smaller steps).
        :param min_features_randomized: The minimum amount of features to randomize when fine-tuning.
        :param random_record_fn: Callback that returns a single random record (numpy array), i.e. all feature values are
                                 random. If None, random records are generated by treating each column in the input
                                 shape as a feature and choosing uniform values [0, 1) for each feature. This default
                                 behaviour is not correct for one-hot-encoded features, and a custom callback which
                                 provides a random record with random one-hot-encoded values should be used instead.
        :param randomize_features_fn: Callback that accepts an existing record (numpy array) and an int which is the
                                      number of features to randomize. The callback should return a new record, where
                                      the specified number of features have been randomized. If None, records are
                                      randomized by treating each column in the input shape as a feature, and choosing
                                      uniform values [0, 1) for each randomized feature. This default behaviour is not
                                      correct for one-hot-encoded features, and a custom callback which randomizes
                                      one-hot-encoded features should be used instead.
        :return: Synthesized record.
        """

        if random_record_fn is None:
            random_record_fn = self._default_random_record
        if randomize_features_fn is None:
            randomize_features_fn = self._default_randomize_features

        k_features_randomized = max_features_randomized
        best_x = None
        best_class_confidence = 0
        num_rejections = 0

        x = random_record_fn()

        for _ in range(max_iterations):
            y = target_classifier.predict(x.reshape(1, -1))[0]
            class_confidence = y[wanted_class]

            if class_confidence >= best_class_confidence:
                # Record accepted, sample randomly
                if class_confidence > min_confidence and np.argmax(y) == wanted_class:
                    if self._rng.random() < class_confidence:
                        return x

                best_x = x
                best_class_confidence = class_confidence
                num_rejections = 0
            else:
                num_rejections += 1
                if num_rejections > max_rejections:
                    # Rejected too many times, we are probably making changes which are too large
                    half_current_features = math.ceil(k_features_randomized / 2)
                    k_features_randomized = max(min_features_randomized, half_current_features)  # type: ignore
                    num_rejections = 0

            x = randomize_features_fn(best_x, k_features_randomized)  # type: ignore

        raise RuntimeError("Failed to synthesize data record")

    def generate_synthetic_shadow_dataset(
        self,
        target_classifier: "CLASSIFIER_TYPE",
        dataset_size: int,
        max_features_randomized: int,
        member_ratio: float = 0.5,
        min_confidence: float = 0.4,
        max_retries: int = 6,
        random_record_fn: Callable[[], np.ndarray] = None,
        randomize_features_fn: Callable[[np.ndarray, int], np.ndarray] = None,
    ) -> Tuple[Tuple[np.ndarray, np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """
        Generates a shadow dataset (member and nonmember samples and their corresponding model predictions) by training
        the shadow models on a synthetic dataset generated from the target classifier using the hill climbing algorithm
        from R. Shokri et al. (2017)

        Paper Link: https://arxiv.org/abs/1610.05820

        :param target_classifier: The classifier to synthesize data from.
        :param dataset_size: How many records to synthesize.
        :param max_features_randomized: The initial amount of features to randomize before fine-tuning. If None, half of
                                        record features will be used, which will not work well for one-hot encoded data.
        :param member_ratio: Percentage of the data that should be used to train the shadow models. Must be between 0
                             and 1.
        :param min_confidence: The minimum confidence the classifier assigns the target class for the record to be
                               accepted (i.e. the hill-climbing algorithm is finished).
        :param max_retries: The maximum amount of record-generation retries. The initial random pick of a record for the
                            hill-climbing algorithm might result in failing to optimize the target-class confidence, and
                            so a new random record will be retried.
        :param random_record_fn: Callback that returns a single random record (numpy array), i.e. all feature values are
                                 random. If None, random records are generated by treating each column in the input
                                 shape as a feature and choosing uniform values [0, 1) for each feature. This default
                                 behaviour is not correct for one-hot-encoded features, and a custom callback which
                                 provides a random record with random one-hot-encoded values should be used instead.
        :param randomize_features_fn: Callback that accepts an existing record (numpy array) and an int which is the
                                      number of features to randomize. The callback should return a new record, where
                                      the specified number of features have been randomized. If None, records are
                                      randomized by treating each column in the input shape as a feature, and choosing
                                      uniform values [0, 1) for each randomized feature. This default behaviour is not
                                      correct for one-hot-encoded features, and a custom callback which randomizes
                                      one-hot-encoded features should be used instead.
        :return: The shadow dataset generated. The shape is `((member_samples, true_label, model_prediction),
                 (nonmember_samples, true_label, model_prediction))`.
        """

        x = []
        y = []

        records_per_class = dataset_size // target_classifier.nb_classes

        # Generate samples for each classification class
        for wanted_class in range(target_classifier.nb_classes):
            one_hot_label = np.zeros(target_classifier.nb_classes)
            one_hot_label[wanted_class] = 1.0

            for _ in range(records_per_class):
                for tries in range(max_retries):
                    try:
                        random_record = self._hill_climbing_synthesis(
                            target_classifier,
                            wanted_class,
                            min_confidence,
                            max_features_randomized=max_features_randomized,
                            random_record_fn=random_record_fn,
                            randomize_features_fn=randomize_features_fn,
                        )
                        break
                    except RuntimeError as err:
                        if tries == max_retries - 1:
                            raise err

                x.append(random_record)
                y.append(one_hot_label)

        return self.generate_shadow_dataset(np.array(x), np.array(y), member_ratio)

    def get_shadow_models(self) -> Sequence["CLASSIFIER_TYPE"]:
        """
        Returns the list of shadow models. `generate_shadow_dataset` or `generate_synthetic_shadow_dataset` must be
        called for the shadow models to be trained.
        """
        return self._shadow_models

    def get_shadow_models_train_sets(self) -> Sequence[Tuple[np.ndarray, np.ndarray]]:
        """
        Returns a list of tuples the form (shadow_x_train, shadow_y_train) for each shadow model.
        `generate_shadow_dataset` or `generate_synthetic_shadow_dataset` must be called before, or a list of Nones will
        be returned.
        """
        return self._shadow_models_train_sets