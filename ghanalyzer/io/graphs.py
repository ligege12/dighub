import os.path
import json
import networkx as nx
from networkx.readwrite import json_graph

import ghanalyzer.models
from ghanalyzer.models import Entity, Account, User, Organization, Repository
from ghanalyzer.utils.jsonline import JsonLineData
from ghanalyzer.io.items import load_accounts, load_repositories


GRAPH_METADATA = {
    'collaborator': {
        'filename': 'Collaborator.jsonl',
        'directed': False,
        'head': {'name': 'repo', 'class': Repository},
        'tail': {'name': 'user', 'class': User},
    },
    'contributor': {
        'filename': 'Contributor.jsonl',
        'directed': False,
        'head': {'name': 'repo', 'class': Repository},
        'tail': {'name': 'user', 'class': User},
    },
    'follow': {
        'filename': 'Follow.jsonl',
        'directed': True,
        'head': {'name': 'followee', 'class': User},
        'tail': {'name': 'follower', 'class': User},
    },
    'membership': {
        'filename': 'Membership.jsonl',
        'directed': False,
        'head': {'name': 'org', 'class': Organization},
        'tail': {'name': 'user', 'class': User},
    },
    'stargazer': {
        'filename': 'Stargazer.jsonl',
        'directed': False,
        'head': {'name': 'repo', 'class': Repository},
        'tail': {'name': 'user', 'class': User},
    },
    'subscriber': {
        'filename': 'Subscriber.jsonl',
        'directed': False,
        'head': {'name': 'repo', 'class': Repository},
        'tail': {'name': 'user', 'class': User},
    },
}


class EntityEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Entity):
            return {'type': type(obj).__name__, 'id': obj.id}
        return json.JSONEncoder.default(self, obj)


def load_graph(path, graph_type, item_filter=None):
    metadata = GRAPH_METADATA.get(graph_type)
    if not metadata:
        return
    path = os.path.join(path, metadata['filename'])
    if metadata['directed']:
        graph = nx.DiGraph()
    else:
        graph = nx.Graph()
    with JsonLineData(path) as data:
        if item_filter is not None:
            data = (x for x in data if item_filter(x))
        for item in data:
            head = item.pop(metadata['head']['name'])
            head = metadata['head']['class'](head['id'])
            tail = item.pop(metadata['tail']['name'])
            tail = metadata['tail']['class'](tail['id'])
            graph.add_edge(tail, head, **item)
    return graph

def load_node_attributes(path, graph):
    repos = load_repositories(path)
    accounts = load_accounts(path)
    for n in graph:
        if isinstance(n, Repository):
            graph.node[n].update(repos[n.id])
        elif isinstance(n, Account):
            graph.node[n].update(accounts[n.id])
        graph.node[n].pop('id')

def _model_from_json(model):
    model_type = model['type']
    model_id = model['id']
    if model_type not in ghanalyzer.models.__all__:
        return
    model_class = getattr(ghanalyzer.models, model_type)
    return model_class(model_id)

def _node_from_json(node):
    node['id'] = _model_from_json(node['id'])
    return node

def read_json_graph(path):
    with open(path) as f:
        data = json.load(f)
        data['nodes'] = list(filter(None, (_node_from_json(x) for x in data['nodes'])))
        return json_graph.node_link_graph(data)

def write_json_graph(path, graph, indent=None):
    data = json_graph.node_link_data(graph)
    with open(path, 'w') as output:
        json.dump(data, output, cls=EntityEncoder, indent=indent)
