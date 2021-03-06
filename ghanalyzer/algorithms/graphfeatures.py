from collections import defaultdict

import numpy as np
import networkx as nx
from scipy.sparse import isspmatrix_csr
from sklearn.preprocessing import StandardScaler

from ghanalyzer.algorithms.graphs import AdjacencyMatrix, BigraphSimilarity
from ghanalyzer.algorithms.features import LanguageVector
from ghanalyzer.algorithms.recommenders import UserCFRecommender, ItemCFRecommender
from ghanalyzer.io import load_accounts, load_repositories, load_repository_languages
from ghanalyzer.models import Entity, User, Repository


__all__ = [
    'DummyFeature', 'ConstantFeature', 'UserFeature', 'RepositoryFeature',
    'LanguageFeature', 'EdgeAttributeFeature', 'CombinedFeature',
    'SimilarityFeature',
]


class EdgeFeature(object):
    feature_count = 0

    def __init__(self, adj):
        assert isinstance(adj, AdjacencyMatrix)
        assert isspmatrix_csr(adj.matrix)

        self.graph = adj.graph
        self.nodes = adj.nodes
        self.node_indices = adj.node_indices
        self.matrix = adj.matrix
        self.indices = adj.matrix.indices
        self.indptr = adj.matrix.indptr
        self.edge_count = adj.matrix.data.size

    def _find_node(self, nodes, node_cls):
        for n in nodes:
            if isinstance(n, node_cls):
                return n

    def _iter_edges(self):
        for r in xrange(len(self.nodes)):
            for i in xrange(self.indptr[r], self.indptr[r+1]):
                c = self.indices[i]
                yield self.nodes[r], self.nodes[c]

    def _create_feature_matrix(self, feature_getter, root=None, standardize=True):
        features = np.zeros((self.edge_count, self.feature_count))
        for i, (u, v) in enumerate(self._iter_edges()):
            feature = feature_getter(u, v, root)
            if feature is None:
                continue
            features[i, :] = feature
        if standardize:
            features = StandardScaler().fit_transform(features)
        return features

    def get_feature_matrix(self, root=None):
        raise NotImplementedError()


class DummyFeature(EdgeFeature):
    feature_count = 0

    def get_feature_matrix(self, root=None):
        return np.zeros((self.edge_count, self.feature_count))


class ConstantFeature(EdgeFeature):
    feature_count = 1

    def __init__(self, adj, value=1):
        super(ConstantFeature, self).__init__(adj)
        self.value = value
        self.features = np.empty((self.edge_count, self.feature_count))
        self.features.fill(self.value)

    def get_feature_matrix(self, root=None):
        return self.features


class NodeFeature(EdgeFeature):
    node_cls = Entity
    keys = []

    def __init__(self, adj, data_path=None, standardize=True):
        super(NodeFeature, self).__init__(adj)
        self.feature_count = len(self.keys)
        if data_path is not None:
            self.entities = self._load_entities(data_path)
        else:
            self.entities = defaultdict(dict)
        self.features = self._create_feature_matrix(self._get_feature, standardize)

    def _load_entities(self, path):
        raise NotImplementedError()

    def _get_feature(self, u, v, root=None):
        target = self._find_node((u, v), self.node_cls)
        if target is None:
            return
        item = self.entities[target.id]
        if target in self.graph:
            item.update(self.graph.node[target])
        feature = [float(item.get(k) or 0) for k in self.keys]
        return feature

    def get_feature_matrix(self, root=None):
        return self.features


class UserFeature(NodeFeature):
    node_cls = User
    keys = ['public_repos', 'public_gists', 'followers', 'following', 'hireable']

    def _load_entities(self, path):
        return load_accounts(path)


class RepositoryFeature(NodeFeature):
    node_cls = Repository
    keys = ['fork', 'open_issues_count', 'has_wiki', 'has_downloads', 'forks_count',
        'has_issues', 'stargazers_count', 'size']

    def _load_entities(self, path):
        return load_repositories(path)


class LanguageFeature(EdgeFeature):
    def __init__(self, adj, data_path, standardize=True):
        super(LanguageFeature, self).__init__(adj)
        self.languages = load_repository_languages(data_path)
        self.languages = LanguageVector(self.languages)
        self.language_features = self.languages.features.toarray()
        self.feature_count = self.features.shape[1]
        self.features = self._create_feature_matrix(self._get_feature, standardize)

    def _get_feature(self, u, v, root=None):
        repo = self._find_node((u, v), Repository)
        if repo is None:
            return
        index = self.languages.sample_indices.get(repo.id, None)
        if index is None:
            return
        return list(self.language_features[index, :])

    def get_feature_matrix(self, root=None):
        return self.features


class EdgeAttributeFeature(EdgeFeature):
    def __init__(self, adj, keys=(), standardize=True):
        super(EdgeAttributeFeature, self).__init__(adj)
        self.keys = keys
        self.feature_count = len(keys)
        self.features = self._create_feature_matrix(self._get_feature, standardize)

    def _get_feature(self, u, v, root=None):
        return [self.graph[u][v].get(k, 1) for k in self.keys]

    def get_feature_matrix(self, root=None):
        return self.features


class CombinedFeature(EdgeFeature):
    def __init__(self, adj, *extractors):
        super(CombinedFeature, self).__init__(adj)
        if not extractors:
            extractors = (DummyFeature(adj),)
        self.extractors = extractors
        self.feature_count = sum(e.feature_count for e in self.extractors)

    def get_feature_matrix(self, root=None):
        features = tuple(e.get_feature_matrix(root) for e in self.extractors)
        return np.concatenate(features, axis=1)


class SimilarityFeature(EdgeFeature):
    feature_count = 2

    def __init__(self, adj, similarity, standardize=True):
        super(SimilarityFeature, self).__init__(adj)
        assert isinstance(similarity, BigraphSimilarity)
        self.similarity = similarity
        self.standardize = standardize

    def get_feature_matrix(self, root=None):
        root = self.nodes[root]
        if root in self.similarity.bigraph.source_indices:
            r = self.similarity.bigraph.source_indices[root]
            source_similarity = self.similarity.ss
            target_similarity = self.similarity.st
        elif root in self.similarity.bigraph.target_indices:
            r = self.similarity.bigraph.target_indices[root]
            source_similarity = self.similarity.ts
            target_similarity = self.similarity.tt
        else:
            raise ValueError('unable to measure similarities between the root node and other nodes')

        node_features = np.zeros((len(self.nodes), 2))
        for s, node in enumerate(self.similarity.bigraph.sources):
            index = self.node_indices.get(node, None)
            if index is not None:
                node_features[index, 0] = source_similarity[r, s]
        for t, node in enumerate(self.similarity.bigraph.targets):
            index = self.node_indices.get(node, None)
            if index is not None:
                node_features[index, 1] = target_similarity[r, t]

        # treat the feature of node v as the feature of edge (u, v)
        features = node_features[self.indices, :]
        if self.standardize:
            features = StandardScaler().fit_transform(features)
        return features
