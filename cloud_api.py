import enum
import inspect
import json
from typing import (
    Callable,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

import grpc
from typing_extensions import ParamSpec
from yandex.cloud.access.access_pb2 import (
    AccessBinding,
    ListAccessBindingsRequest,
    SetAccessBindingsRequest,
    Subject,
)
from yandex.cloud.compute.v1.instance_pb2 import Instance as YaInstance
from yandex.cloud.compute.v1.instance_service_pb2 import (
    GetInstanceRequest,
    ListInstancesRequest,
    RestartInstanceRequest,
    StartInstanceRequest,
    StopInstanceRequest,
)
from yandex.cloud.compute.v1.instance_service_pb2_grpc import InstanceServiceStub
from yandex.cloud.mdb.postgresql.v1.cluster_pb2 import Cluster
from yandex.cloud.mdb.postgresql.v1.cluster_service_pb2 import (
    GetClusterRequest,
    ListClustersRequest,
    StartClusterRequest,
    StopClusterRequest,
)
from yandex.cloud.mdb.postgresql.v1.cluster_service_pb2_grpc import ClusterServiceStub
from yandex.cloud.serverless.functions.v1.function_pb2 import Function
from yandex.cloud.serverless.functions.v1.function_service_pb2 import (
    GetFunctionRequest,
    ListFunctionsRequest,
)
from yandex.cloud.serverless.functions.v1.function_service_pb2_grpc import FunctionServiceStub

# todo: why this is not work in pycharm?
T = TypeVar('T')  # noqa: VNE001
P = ParamSpec('P')  # noqa: VNE001


def call_grpc(func: Callable[P, T]) -> Union[Callable[P, Optional[str]],
                                             Callable[P, Tuple[Optional[str], T]]]:
    from main import sdk  # todo: shit

    func_signature = inspect.signature(func)
    func_return = func_signature.return_annotation
    stub = func_signature.parameters['client'].annotation

    if func_return is None or func_return is func_signature.empty:
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> Optional[str]:
            client = sdk.client(stub)
            try:
                func(client, *args, **kwargs)
            except grpc.RpcError as e:
                if hasattr(e, 'details'):
                    return e.details()
                return json.dumps(e.args)
    else:
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> Tuple[Optional[str], T]:
            client = sdk.client(stub)
            try:
                return func(client, *args, **kwargs), None
            except grpc.RpcError as e:
                if hasattr(e, 'details'):
                    return None, e.details()
                return None, json.dumps(e.args)

    return wrapped


@call_grpc
def vm_list(client: InstanceServiceStub, folder_id: str) -> List[YaInstance]:
    resp = client.List(ListInstancesRequest(folder_id=folder_id))
    return list(resp.instances)


@call_grpc
def vm_get(client: InstanceServiceStub, vm_id: str) -> YaInstance:
    return client.Get(GetInstanceRequest(instance_id=vm_id))


@call_grpc
def vm_start(client: InstanceServiceStub, vm_id: str) -> None:
    client.Start(StartInstanceRequest(instance_id=vm_id))


@call_grpc
def vm_stop(client: InstanceServiceStub, vm_id: str) -> None:
    client.Stop(StopInstanceRequest(instance_id=vm_id))


@call_grpc
def vm_restart(client: InstanceServiceStub, vm_id: str) -> None:
    client.Restart(RestartInstanceRequest(instance_id=vm_id))


@call_grpc
def pg_list(client: ClusterServiceStub, folder_id: str) -> List[Cluster]:
    resp = client.List(ListClustersRequest(folder_id=folder_id))
    return list(resp.clusters)


@call_grpc
def pg_get(client: ClusterServiceStub, cluster_id: str) -> Cluster:
    return client.Get(GetClusterRequest(cluster_id=cluster_id))


@call_grpc
def pg_start(client: ClusterServiceStub, cluster_id: str) -> None:
    client.Start(StartClusterRequest(cluster_id=cluster_id))


@call_grpc
def pg_stop(client: ClusterServiceStub, cluster_id: str) -> None:
    client.Stop(StopClusterRequest(cluster_id=cluster_id))


@call_grpc
def func_list(client: FunctionServiceStub, folder_id: str) -> List[Tuple[Function, bool]]:
    resp = client.List(ListFunctionsRequest(folder_id=folder_id))
    funcs = []
    for func in resp.functions:
        bindings_resp = client.ListAccessBindings(ListAccessBindingsRequest(resource_id=func.id))
        funcs.append((func, bool(bindings_resp.access_bindings)))
    return funcs


@call_grpc
def func_get(client: FunctionServiceStub, func_id: str) -> Function:
    return client.Get(GetFunctionRequest(function_id=func_id))


@call_grpc
def func_open(client: FunctionServiceStub, func_id: str) -> None:
    client.SetAccessBindings(SetAccessBindingsRequest(
        resource_id=func_id,
        access_bindings=[
            AccessBinding(
                role_id='serverless.functions.invoker',
                subject=Subject(
                    id='allUsers',
                    type='system',
                ),
            ),
        ],
    ))


@call_grpc
def func_close(client: FunctionServiceStub, func_id: str) -> None:
    client.SetAccessBindings(SetAccessBindingsRequest(
        resource_id=func_id,
        access_bindings=[],
    ))
