"""
services/pipeline-worker/src/pipeline/dag_builder.py

Directed Acyclic Graph (DAG) construction and execution order for pipeline stages.
Spec §4.2.
"""
import networkx as nx


def build_dag(stages: list[dict]) -> nx.DiGraph:
    """
    Builds a directed dependency graph from stage definitions.
    Stages without explicit dependsOn implicitly depend on the previous stage
    (build -> test -> canary_deploy -> progressive_verify).
    """
    dag = nx.DiGraph()
    prev_stage = None
    for stage in stages:
        name = stage["name"]
        dag.add_node(name, config=stage)
        deps = stage.get("dependsOn", [prev_stage] if prev_stage else [])
        for dep in deps:
            if dep:
                dag.add_edge(dep, name)
        prev_stage = name

    if not nx.is_directed_acyclic_graph(dag):
        raise ValueError("Pipeline stages contain a cycle")
    return dag


def execution_order(dag: nx.DiGraph) -> list[str]:
    """Topological sort — the order the stage runner executes stages in."""
    return list(nx.topological_sort(dag))
