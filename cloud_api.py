import enum
from functools import wraps
from typing import (
    List,
    Optional,
    TypeVar,
)

import grpc
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

T = TypeVar('T')  # noqa: VNE001


class AppException(Exception):
    def format(self) -> str:  # noqa: A003
        if isinstance(self.__context__, grpc.Call):
            err = self.__context__
            return f'{err.code().name}: {err.details()}'
        return str(self)


class BaseRepo:
    def __init__(self, folder_id: str) -> None:
        self._folder_id = folder_id

    @classmethod
    def _is_not_found(cls, error: grpc.RpcError):
        return isinstance(error, grpc.Call) and error.code() in (grpc.StatusCode.INVALID_ARGUMENT,
                                                                 grpc.StatusCode.NOT_FOUND)

    @staticmethod
    def call_grpc(method: T) -> T:
        @wraps(method)
        def wrapped(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except grpc.RpcError as e:
                raise AppException from e
        return wrapped


class InstanceStatus(enum.Enum):
    RUNNING = 'running'
    IN_PROGRESS = 'in-progress'
    STOPPED = 'stopped'
    ERROR = 'error'


class Instance:
    _STATUS_MAP = {
        YaInstance.PROVISIONING: InstanceStatus.IN_PROGRESS,
        YaInstance.RUNNING: InstanceStatus.RUNNING,
        YaInstance.STOPPING: InstanceStatus.IN_PROGRESS,
        YaInstance.STOPPED: InstanceStatus.STOPPED,
        YaInstance.STARTING: InstanceStatus.IN_PROGRESS,
        YaInstance.RESTARTING: InstanceStatus.IN_PROGRESS,
        YaInstance.UPDATING: InstanceStatus.IN_PROGRESS,
        YaInstance.ERROR: InstanceStatus.ERROR,
        YaInstance.CRASHED: InstanceStatus.ERROR,
        YaInstance.DELETING: InstanceStatus.IN_PROGRESS,
    }

    def __init__(self, ya_instance: YaInstance):
        self._instance = ya_instance
        self.id: str = ya_instance.id
        self.name: str = ya_instance.name
        self.status = self._STATUS_MAP[ya_instance.status]
        self.public_ips: List[str] = []

        for net in ya_instance.network_interfaces:
            if net.primary_v4_address.one_to_one_nat:
                self.public_ips.append(net.primary_v4_address.one_to_one_nat.address)


class InstanceRepo(BaseRepo):
    def __init__(self, folder_id: str, client: InstanceServiceStub) -> None:
        super().__init__(folder_id)
        self._client = client

    @BaseRepo.call_grpc
    def get_list(self, filter_: Optional[str] = None) -> List[Instance]:
        resp = self._client.List(ListInstancesRequest(folder_id=self._folder_id, filter=filter_))
        return [Instance(ya_instance) for ya_instance in resp.instances]

    @BaseRepo.call_grpc
    def get_single(self, id_or_name: str) -> Instance:
        try:
            ya_instance = self._client.Get(GetInstanceRequest(instance_id=id_or_name))
        except grpc.RpcError as err:
            if not self._is_not_found(err):
                raise
            instances = self.get_list(filter_=f'name="{id_or_name}"')
            if not instances:
                raise AppException('instance not found')
            return instances[0]
        return Instance(ya_instance)

    @BaseRepo.call_grpc
    def start(self, id_: str) -> None:
        self._client.Start(StartInstanceRequest(instance_id=id_))

    @BaseRepo.call_grpc
    def stop(self, id_: str) -> None:
        self._client.Stop(StopInstanceRequest(instance_id=id_))

    @BaseRepo.call_grpc
    def restart(self, id_: str) -> None:
        self._client.Restart(RestartInstanceRequest(instance_id=id_))


class PostgresClusterStatus(enum.Enum):
    RUNNING = 'running'
    IN_PROGRESS = 'in-progress'
    STOPPED = 'stopped'
    ERROR = 'error'
    UNKNOWN = 'unknown'


class PostgresCluster:
    _STATUS_MAP = {
        Cluster.STATUS_UNKNOWN: PostgresClusterStatus.UNKNOWN,
        Cluster.CREATING: PostgresClusterStatus.IN_PROGRESS,
        Cluster.STARTING: PostgresClusterStatus.IN_PROGRESS,
        Cluster.RUNNING: PostgresClusterStatus.RUNNING,
        Cluster.ERROR: PostgresClusterStatus.ERROR,
        Cluster.UPDATING: PostgresClusterStatus.IN_PROGRESS,
        Cluster.STOPPING: PostgresClusterStatus.IN_PROGRESS,
        Cluster.STOPPED: PostgresClusterStatus.STOPPED,
    }

    def __init__(self, pg: Cluster):
        self._pg = pg
        self.id: str = pg.id
        self.name: str = pg.name
        self.status = self._STATUS_MAP[pg.status]


class PostgresRepo(BaseRepo):
    def __init__(self, folder_id: str, client: ClusterServiceStub) -> None:
        super().__init__(folder_id)
        self._client = client

    @BaseRepo.call_grpc
    def get_list(self, filter_: Optional[str] = None) -> List[PostgresCluster]:
        resp = self._client.List(ListClustersRequest(folder_id=self._folder_id, filter=filter_))
        return [PostgresCluster(pg) for pg in resp.clusters]

    @BaseRepo.call_grpc
    def get_single(self, id_or_name: str) -> PostgresCluster:
        try:
            pg = self._client.Get(GetClusterRequest(cluster_id=id_or_name))
        except grpc.RpcError as err:
            if not self._is_not_found(err):
                raise
            pgs = self.get_list(filter=f'name="{id_or_name}"')
            if not pgs:
                raise AppException('pg cluster not found')
            return pgs[0]
        return PostgresCluster(pg)

    @BaseRepo.call_grpc
    def start(self, id_: str) -> None:
        self._client.Start(StartClusterRequest(cluster_id=id_))

    @BaseRepo.call_grpc
    def stop(self, id_: str) -> None:
        self._client.Stop(StopClusterRequest(cluster_id=id_))


class ServerlessFunctionStatus(enum.Enum):
    OPENED = 'opened'
    CLOSED = 'closed'


class ServerlessFunction:
    def __init__(self, function: Function, is_opened: bool):
        self._function = function
        self.id: str = function.id
        self.name: str = function.name
        self.status = (ServerlessFunctionStatus.OPENED
                       if is_opened else ServerlessFunctionStatus.CLOSED)
        self.invoke_url = function.http_invoke_url


class ServerlessFunctionRepo(BaseRepo):
    def __init__(self, folder_id: str, client: FunctionServiceStub) -> None:
        super().__init__(folder_id)
        self._client = client

    def _get_bindings(self, function_id: str) -> List[AccessBinding]:
        bindings_resp = self._client.ListAccessBindings(
            ListAccessBindingsRequest(resource_id=function_id),
        )
        return list(bindings_resp.access_bindings)

    @BaseRepo.call_grpc
    def get_list(self, filter_: Optional[str] = None) -> List[ServerlessFunction]:
        resp = self._client.List(ListFunctionsRequest(folder_id=self._folder_id, filter=filter_))
        funcs = []
        for func in resp.functions:
            is_opened = bool(self._get_bindings(func.id))
            funcs.append(ServerlessFunction(func, is_opened))
        return funcs

    @BaseRepo.call_grpc
    def get_single(self, id_or_name: str) -> ServerlessFunction:
        try:
            func = self._client.Get(GetFunctionRequest(function_id=id_or_name))
        except grpc.RpcError as err:
            if not self._is_not_found(err):
                raise
            funcs = self.get_list(filter_=f'name="{id_or_name}"')
            if not funcs:
                raise AppException('function not found')
            return funcs[0]
        is_opened = bool(self._get_bindings(func.id))
        return ServerlessFunction(func, is_opened)

    @BaseRepo.call_grpc
    def open(self, id_: str) -> None:  # noqa: A003
        self._client.SetAccessBindings(SetAccessBindingsRequest(
            resource_id=id_,
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

    @BaseRepo.call_grpc
    def close(self, id_: str) -> None:
        self._client.SetAccessBindings(SetAccessBindingsRequest(
            resource_id=id_,
            access_bindings=[],
        ))
