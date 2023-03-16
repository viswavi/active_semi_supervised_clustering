
from active_semi_clustering.exceptions import EmptyClustersException
import numpy as np
import random
import scipy.spatial.distance
from sklearn.metrics.pairwise import euclidean_distances
from sklearn.preprocessing import normalize
from sklearn.utils import check_random_state
from sklearn.utils.extmath import row_norms

from cmvc.Multi_view_CH_kmeans import init_seeded_kmeans_plusplus

import time


class KMeans:
    def __init__(self, n_clusters=3, max_iter=100, num_reinit=1, normalize_vectors=False, split_normalization=False, init="random", verbose=False):
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.normalize_vectors = normalize_vectors
        self.split_normalization = split_normalization
        assert init in ["random", "k-means++"]
        self.init = init
        self.verbose = verbose
        self.num_reinit = num_reinit

    def fit(self, X, y=None, **kwargs):
        # Initialize cluster centers

        X_mean = X.mean(axis=0)
        X -= X_mean

        min_inertia = np.inf
        for random_seed in range(self.num_reinit):
            start = time.perf_counter()
            original_start = start
            cluster_centers = self._init_cluster_centers(X, y, random_seed=random_seed)
            elapsed = time.perf_counter() - start
            if self.verbose:
                print(f"{self.init} k-means initialization took {round(elapsed, 4)} seconds.")

            # Repeat until convergence
            cluster_centers_shift = np.zeros(cluster_centers.shape)

            for iteration in range(self.max_iter):
                # print(f"iteration {iteration}")
                timer_dict = {}
                timer = time.perf_counter()
                if self.normalize_vectors:
                    if self.split_normalization:
                        kg_centers = normalize(cluster_centers[:, :300], axis=1, norm="l2")
                        bert_centers = normalize(cluster_centers[:, 300:], axis=1, norm="l2")
                        cluster_centers = np.hstack([kg_centers, bert_centers])
                    else:
                        cluster_centers = normalize(cluster_centers, axis=1, norm="l2")
                    timer_dict["Centroid normalization"] = round(time.perf_counter() - timer, 3)
                    timer = time.perf_counter()

                prev_cluster_centers = cluster_centers.copy()
                prev_cluster_centers_shift = cluster_centers_shift.copy()
                timer_dict["Copy Centroids"] = round(time.perf_counter() - timer, 3)
                timer = time.perf_counter()

                # Assign clusters
                labels = self._assign_clusters(X, y, cluster_centers, self._dist).copy()
                timer_dict["Assign clusters"] = round(time.perf_counter() - timer, 3)
                timer = time.perf_counter()

                # Estimate means
                cluster_centers = self._get_cluster_centers(X, labels).copy()
                timer_dict["Estimate cluster centers"] = round(time.perf_counter() - timer, 3)
                timer = time.perf_counter()

                # Check for convergence
                cluster_centers_shift = (prev_cluster_centers - cluster_centers)
                converged = np.allclose(cluster_centers_shift, np.zeros(cluster_centers.shape), atol=1e-6, rtol=0)
                timer_dict["Check convergence"] = round(time.perf_counter() - timer, 3)
                timer = time.perf_counter()
                # print(f"K-Means iteration {iteration} took {round(time.perf_counter() - original_start, 3)} seconds.")

                # print(f"Timer dict: {timer_dict}")

                if converged: break

            inertia = 0
            for row_idx in range(len(X)):
                assigned_cluster_center = cluster_centers[labels[row_idx]]
                inertia += scipy.spatial.distance.euclidean(X[row_idx], assigned_cluster_center)

            if inertia <= min_inertia:
                min_inertia = inertia
                self.cluster_centers_, self.labels_ = cluster_centers, labels
                self.inertia = inertia

        return self

    def _init_cluster_centers(self, X, y=None, seed_set = None, duplicate_eps = 1e-8, random_seed=0):
        random_state = np.random.RandomState(random_seed)
        assert self.n_clusters <= len(X)
        x_squared_norms = row_norms(X, squared=True)

        if self.init == "random":
            remaining_row_idxs = list(range(len(X)))
            seeds = np.empty((self.n_clusters, X.shape[1]))
            seeds[:] = np.nan
            for i in range(self.n_clusters):
                while True:
                    sampled_idx = random.choice(remaining_row_idxs)
                    sampled_vector = X[sampled_idx]
                    distance_to_seeds = np.linalg.norm(seeds - sampled_vector, axis=1)
                    unique = False
                    if i == 0:
                        unique = True
                    else:
                        duplicate_found = np.min(distance_to_seeds[np.logical_not(np.isnan(distance_to_seeds))]) < duplicate_eps
                        if not duplicate_found:
                            unique = True
                    remaining_row_idxs.remove(sampled_idx)
                    if unique:
                        seeds[i] = sampled_vector
                        break
        else:
            # Use k-means++ (https://en.wikipedia.org/wiki/K-means%2B%2B#Improved_initialization_algorithm) to 
            # initialize the cluster centers.

            # Using the same method as described by Arthur and Vassilvitskii (2007), we choose `n_local_trials`
            # top candidates for the next cluster center and select the one among these which will most reduce
            # the sum total distance to the existing set of cluster centers.
            n_local_trials = 2 + int(np.log(self.n_clusters))

            # This is an expensive >quadratic operation which will be very slow for large datasets.
            cluster_seeds = []
            remaining_row_idxs = list(range(len(X)))
            if seed_set is None:
                # Pick initial cluster center.
                sampled_idx = random_state.choice(remaining_row_idxs)
                seed_set = [X[sampled_idx]]
                cluster_seeds.append(X[sampled_idx])
            else:
                cluster_seeds.extend(seed_set)

            closest_dist_sq_all = euclidean_distances(seed_set, X, Y_norm_squared=x_squared_norms, squared=True)
            closest_dist_sq = np.min(closest_dist_sq_all, axis=0)
            for i in range(len(cluster_seeds), self.n_clusters):
                nearest_distances_normalized = closest_dist_sq / sum(closest_dist_sq)
                assert len(nearest_distances_normalized.shape) == 1
                assert len(remaining_row_idxs) == len(nearest_distances_normalized)

                # Try out the top 'n_local_trials' choices for the next seed, and choose the one with least
                # average distance to other points in the dataset.
                candidate_ids = random_state.choice(remaining_row_idxs, p=nearest_distances_normalized, size=n_local_trials)
                distance_to_candidates = euclidean_distances(X[candidate_ids], X, Y_norm_squared=x_squared_norms, squared=True)
                min_remaining_distance_to_candidates = np.minimum(closest_dist_sq, distance_to_candidates)
                candidate_potentials = min_remaining_distance_to_candidates.sum(axis=1)
                best_candidate = np.argmin(candidate_potentials)

                # The `closest_dist_sq` array should contain the distance from each point in
                # the dataset to its closest seed point.
                closest_dist_sq = min_remaining_distance_to_candidates[best_candidate]
                cluster_seeds.append(X[candidate_ids[best_candidate]])
            seeds = np.vstack(cluster_seeds)
        return seeds

    def _dist(self, x, y):
        return np.sqrt(np.sum((x - y) ** 2))

    def _assign_clusters(self, X, y, cluster_centers, dist):
        labels = np.full(X.shape[0], fill_value=-1)

        for i, x in enumerate(X):
            labels[i] = np.argmin([dist(x, c) for c in cluster_centers])

        # Handle empty clusters
        # See https://github.com/scikit-learn/scikit-learn/blob/0.19.1/sklearn/cluster/_k_means.pyx#L309
        n_samples_in_cluster = np.bincount(labels, minlength=self.n_clusters)
        empty_clusters = np.where(n_samples_in_cluster == 0)[0]

        if len(empty_clusters) > 0:
            raise EmptyClustersException

        return labels

    def _get_cluster_centers(self, X, labels):
        return np.array([X[labels == i].mean(axis=0) for i in range(self.n_clusters)])
