import random
from itertools import izip

import numpy as np
import networkx as nx
from scipy.sparse import coo_matrix, dia_matrix
from scipy.optimize import fmin_l_bfgs_b
from sklearn.preprocessing import normalize

from ghanalyzer.algorithms.graphfeatures import UserFeature, RepositoryFeature, BigraphEdgeFeature
from ghanalyzer.models import User, Repository
from ghanalyzer.utils.recommendation import recommend_by_rank


def sigmoid(x):
    return 1 / (1 + np.exp(-x))

class SupervisedRWRecommender(object):
    """recommender based on Supervised Random Walk (WSDM 2011)"""
    def __init__(self, data_path, max_steps=100, alpha=0.85, lambda_=0.01, epsilon=0.01, loss_width=1.0, weight_key=None):
        self.data_path = data_path
        self.max_steps = max_steps
        self.alpha = float(alpha)
        self.lambda_ = float(lambda_)
        self.epsilon = float(epsilon)
        self.loss_width = float(loss_width)
        self.weight_key = weight_key

    def _iter_edges(self):
        for r, c in izip(self.row, self.col):
            u, v = self.nodes[r], self.nodes[c]
            yield u, v, self.graph[u][v]

    def train(self, graph):
        self.graph = graph
        self.candidates = [n for n in self.graph if isinstance(n, Repository)]
        self.nodes = self.graph.nodes()
        self.node_indices = {n: i for i, n in enumerate(self.nodes)}
        self.adjacency = nx.to_scipy_sparse_matrix(self.graph, nodelist=self.nodes,
            weight=self.weight_key, format='coo')
        self.row = self.adjacency.row.astype(np.int)
        self.col = self.adjacency.col.astype(np.int)
        self.feature_extractor = BigraphEdgeFeature(self.graph,
            source_cls=User, target_cls=Repository,
            source_extractor=UserFeature(self.data_path),
            target_extractor=RepositoryFeature(self.data_path),
            weight_key=self.weight_key)
        self.features = self.feature_extractor.get_feature_matrix(self._iter_edges())
        self.N = len(self.nodes)
        self.E, self.M = self.features.shape

    def recommend(self, user, n):
        rank = self._get_rank(user)
        rank = {k: v for k, v in izip(self.nodes, rank) \
            if isinstance(k, Repository) and k not in self.graph[user]}
        return recommend_by_rank(rank, n)

    def _get_edge_strength(self, w):
        S = np.einsum('ij,j->i', self.features, w)
        A = sigmoid(S)
        dA = A * (1 - A)
        dA = dA[:, np.newaxis] * self.features
        return A, dA

    def _get_transition_probability(self, A, root):
        coo = (self.row, self.col)
        shape = (self.N, self.N)
        Q = coo_matrix((A, coo), shape=shape)
        Q = normalize(Q, norm='l1')
        Q *= 1 - self.alpha
        Q = Q.tolil()
        for i in xrange(self.N):
            Q[i, root] += self.alpha
        return Q.tocsc()

    def _get_transition_probability_derivative(self, A, dA):
        coo = (self.row, self.col)
        shape = (self.N, self.N)
        F = coo_matrix((A, coo), shape=shape).tocsr()
        norm_F = dia_matrix((F.sum(axis=1).T, (0,)), shape=shape)
        
        dQ = np.empty((self.M,), dtype=object)
        for m in xrange(self.M):
            dFm = coo_matrix((dA[:, m], coo), shape=shape).tocsr()
            norm = dFm.sum(axis=1)
            norm_dFm = dia_matrix((norm.T, (0,)), shape=shape)
            denominator = 1.0 / np.power(norm, 2)
            denominator = dia_matrix((denominator.T, (0,)), shape=shape)
            
            dQ[m] = norm_F * dFm - norm_dFm * F
            dQ[m] = denominator * dQ[m]
            dQ[m] *= 1 - self.alpha
            dQ[m] = dQ[m].tocsc()
        
        return dQ

    def _converged(self, X1, X2):
        delta = np.abs(X2 - X1)
        delta = np.max(delta)
        return delta < self.epsilon, delta

    def _get_stationary_distribution(self, Q):
        P = np.empty((self.N,))
        P.fill(1.0 / self.N)
        converged, delta = False, 0.0
        for _ in xrange(self.max_steps):
            P1 = Q.T.dot(P.T).T
            P1 /= P1.sum()
            converged, delta = self._converged(P, P1)
            if converged:
                break
            P = P1
        if not converged:
            print 'Warning: stationary distribution does not converge ' \
                'in %d iteration(s) (delta=%f)' % (self.max_steps, delta)
        return P

    def _get_stationary_distribution_derivative(self, P, Q, dQ):
        shape = (self.N, self.N)
        dP = np.zeros((self.N, self.M))
        for m in xrange(self.M):
            converged, delta = False, 0.0
            for _ in xrange(self.max_steps):
                diag_dP = dia_matrix((dP[:, m], (0,)), shape=shape)
                diag_P = dia_matrix((P, (0,)), shape=shape)
                dPm = diag_dP * Q + diag_P * dQ[m]
                dPm = dPm.sum(axis=0)
                converged, delta = self._converged(dP[:, m], dPm)
                if converged:
                    break
                dP[:, m] = dPm
            if not converged:
                print 'Warning: stationary distribution derivative does not converge ' \
                    'in %d iteration(s) (delta=%f)' % (self.max_steps, delta)
        return dP

    def _loss_function(self, w, root, pairs):
        A, dA = self._get_edge_strength(w)
        Q = self._get_transition_probability(A, root)
        dQ = self._get_transition_probability_derivative(A, dA)
        P = self._get_stationary_distribution(Q)
        dP = self._get_stationary_distribution_derivative(P, Q, dQ)

        diff = np.array([P[u] - P[v] for u, v in pairs])
        loss = sigmoid(diff / self.loss_width)
        objective = np.sum(w ** 2) + self.lambda_ * np.sum(loss)
        ddiff = loss * (1 - loss) / self.loss_width
        dpairs = np.empty((len(pairs), self.M))
        for i, (u, v) in enumerate(pairs):
            dpairs[i, :] = dP[u, :] - dP[v, :]
        gradient = 2 * w + np.einsum('i,im->m', ddiff, dpairs) * self.lambda_
        return objective, gradient
    
    def _select_samples(self, user):
        positive = self.graph.neighbors(user)
        others = set(self.candidates) - set(positive)
        negative = random.sample(others, min(len(positive), len(others)))
        positive = [self.node_indices[x] for x in positive]
        negative = [self.node_indices[x] for x in negative]
        return positive, negative

    def _get_rank(self, user):
        positive, negative = self._select_samples(user)
        pairs = [(u, v) for u in negative for v in positive]
        u = self.node_indices[user]

        w0 = np.random.rand(self.M)
        w, _, _ = fmin_l_bfgs_b(self._loss_function, w0, args=(u, pairs), iprint=0)
        A, _ = self._get_edge_strength(w)
        Q = self._get_transition_probability(A, u)
        P = self._get_stationary_distribution(Q)
        return P