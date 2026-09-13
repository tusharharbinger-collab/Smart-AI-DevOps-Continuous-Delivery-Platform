"""
services/pipeline-worker/src/tasks/kind_loader.py

Loads a freshly built image directly into every node of the local Kind
cluster, for pipelines with no registry credential configured — the common
case for local development, where there is no real OCI registry to push to
(see preflight.py's docstring: this stack has historically built images
locally and relied on `make deploy-sample-app` manually loading them).

This reimplements what the `kind` CLI's `load docker-image` command does
under the hood — `docker save <image> | docker exec -i <node> ctr
--namespace=k8s.io images import -` — using the `docker` Python SDK
directly rather than shelling out, because this container has no `kind`
binary and (per build_task.py's own docstring) no `docker` CLI either, only
the SDK talking to the mounted `/var/run/docker.sock`. Kind nodes run
containerd, not another Docker daemon, which is why the import target is
`ctr` (containerd's CLI, already present in every Kind node image) inside
the `k8s.io` namespace — the same namespace the kubelet's own containerd
client reads images from.

Node discovery is by the `io.x-k8s.kind.cluster` label Kind itself sets on
every node container, not a hardcoded cluster name, so this works with
whatever cluster name `k8s/kind-config.yaml` declares.
"""
import socket as socket_module

import docker
import structlog

logger = structlog.get_logger(__name__)

# Generous but bounded — an image built by this pipeline is at most a few
# hundred MB; a hang here almost certainly means the node's containerd is
# wedged, not that more time would help.
_IMPORT_TIMEOUT_SECONDS = 120


class KindLoadError(Exception):
    pass


def kind_load_image(image_ref: str, pipeline_run_id: str) -> None:
    """
    Saves `image_ref` (already built into this container's Docker daemon)
    as an OCI tar stream and imports it into every Kind node's containerd
    `k8s.io` namespace, so a Deployment referencing this exact tag can be
    scheduled without ever reaching an external registry.

    Raises KindLoadError with a clear reason on any failure — a silent
    no-op here would surface later as an opaque ImagePullBackOff with no
    link back to this stage.
    """
    client = docker.from_env()

    nodes = client.containers.list(filters={"label": "io.x-k8s.kind.cluster"})
    if not nodes:
        raise KindLoadError(
            "No Kind cluster node containers found (label io.x-k8s.kind.cluster). "
            "Is `make kind-up` running? If you're only testing against docker-compose "
            "without a Kind cluster, attach a registry credential to the project instead "
            "so the image is pushed rather than loaded locally."
        )

    try:
        image = client.images.get(image_ref)
    except docker.errors.ImageNotFound as e:
        raise KindLoadError(f"Image {image_ref!r} not found in the local Docker daemon after build: {e}") from e

    # `image.save()` streams a tar (OCI/docker-archive format) of the image
    # and all its layers — the same format `docker save` writes to a file.
    tar_chunks = list(image.save(named=True))

    for node in nodes:
        _import_into_node(client, node, tar_chunks, image_ref)
        logger.info("kind_image_loaded", node=node.name, image=image_ref, pipeline_run_id=pipeline_run_id)


def _import_into_node(client: "docker.DockerClient", node, tar_chunks: list[bytes], image_ref: str) -> None:
    """
    Runs `ctr --namespace=k8s.io images import -` inside one node container,
    with the saved image tar piped to its stdin over the raw exec socket —
    docker-py's high-level API has no "exec with stdin" helper, so this uses
    the low-level `APIClient` exec_create/exec_start(socket=True) pair
    directly, the same primitive `docker exec -i` itself is built on.

    Verified live against a real 3-node Kind cluster with a genuinely
    locally-built image (docker.APIClient.images.build, matching what
    build_task.py actually produces): `docker-py`'s `exec_start(socket=True)`
    returns a `socket.SocketIO`, whose `._sock` is the real underlying
    `socket.socket` — writing the full tar via `sendall()`, a clean
    `shutdown(SHUT_WR)`, and draining `recv()` until a true empty-bytes EOF
    (not bailing out on the first exception) is what makes this reliable;
    closing the socket before EOF truncated the transfer in earlier testing.
    """
    api = client.api
    exec_id = api.exec_create(
        node.id,
        ["ctr", "--namespace=k8s.io", "images", "import", "-"],
        stdin=True,
        stdout=True,
        stderr=True,
    )["Id"]

    sock = api.exec_start(exec_id, socket=True)
    raw_sock = sock._sock  # noqa: SLF001 — docker-py exposes no public accessor to the raw duplex socket
    raw_sock.settimeout(_IMPORT_TIMEOUT_SECONDS)

    output = b""
    try:
        for chunk in tar_chunks:
            raw_sock.sendall(chunk)
        raw_sock.shutdown(socket_module.SHUT_WR)

        while True:
            try:
                data = raw_sock.recv(65536)
            except socket_module.timeout as e:
                raise KindLoadError(
                    f"Timed out after {_IMPORT_TIMEOUT_SECONDS}s waiting for `ctr images import` "
                    f"to finish on node {node.name} while loading {image_ref!r}."
                ) from e
            if not data:
                break
            output += data
    finally:
        sock.close()

    result = api.exec_inspect(exec_id)
    if result.get("ExitCode") not in (0, None):
        raise KindLoadError(
            f"`ctr images import` failed on node {node.name} (exit {result.get('ExitCode')}) "
            f"loading {image_ref!r}: {output.decode(errors='replace')[-500:]}"
        )
