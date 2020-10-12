import json
import os
from typing import Optional, Tuple

import grpc
import telebot
import yandexcloud
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from yandex.cloud.access.access_pb2 import (
    AccessBinding,
    ListAccessBindingsRequest,
    SetAccessBindingsRequest,
    Subject,
)
from yandex.cloud.compute.v1.instance_pb2 import Instance
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

# TODO:
#   отлов ошибок
#     grpc
#     хендлеры
#     sentry
#   git
#     auto deploy
#   логирование
#   описание команд
#   команды
#     установка хука
#     установка команд
#     выключение хука и функции
#   управление функциями
#     вкл/выкл доступ
#   управление виртуалками
#     сделать ip статическим / динамическим
#   управление группами вм
#     вкл / выкл
#     параметры масштабирования
#   контроль доступа
#     юзер
#     верификация запрашивающего
#   добавить poll режим
#   рестуктурировать файлы
#     размотать лапшу в хендлере
#     регистрация команды декоратором

API_TOKEN = os.getenv('TOKEN')
FOLDER = os.getenv('FOLDER')
bot = telebot.TeleBot(API_TOKEN)

INSTANCE_STATUS_EMOJI = {
    Instance.PROVISIONING: '🔵',
    Instance.RUNNING: '\U0001f7e2',
    Instance.STOPPING: '🔴',
    Instance.STOPPED: '🔴',
    Instance.STARTING: '🔵',
    Instance.RESTARTING: '🔵',
    Instance.UPDATING: '🔵',
    Instance.ERROR: '❗',
    Instance.CRASHED: '❗️',
    Instance.DELETING: '🗑',
}
CLUSTER_STATUS_EMOJI = {
    Cluster.STATUS_UNKNOWN: '❔',
    Cluster.CREATING: '🔵',
    Cluster.RUNNING: '\U0001f7e2',
    Cluster.ERROR: '❗',
    Cluster.UPDATING: '🔵',
    Cluster.STOPPING: '🔴',
    Cluster.STOPPED: '🔴',
    Cluster.STARTING: '🔵',
}
FUNCTION_STATUS_EMOJI = {
    True: '\U0001f7e2',
    False: '🔴',
}


def handler(event: dict, _context):
    update = Update.de_json(event['body'])
    print(update)
    bot.process_new_updates([update])
    return {
        'statusCode': 200,
    }


@bot.message_handler(commands=['start', 'help'])
def handle_help(message):
    bot.send_message(message.chat.id, 'to be continued...')


@bot.message_handler(commands=['test'])
def handle_test(message):
    mu = InlineKeyboardMarkup()
    mu.add(
        InlineKeyboardButton('button 1', callback_data='1:1'),
        InlineKeyboardButton('button 2', callback_data='2:1'),
    )
    bot.send_message(message.chat.id, 'message', reply_markup=mu)


@bot.message_handler(commands=['vms', 'vm'])
def handle_vms(message):
    sdk = yandexcloud.SDK()
    compute = sdk.client(InstanceServiceStub)
    resp = compute.List(ListInstancesRequest(folder_id=FOLDER))
    mu = InlineKeyboardMarkup()
    text = "выбери ВМ"
    for vm in resp.instances:
        status_name = Instance.Status.Name(vm.status)
        status_emoji = INSTANCE_STATUS_EMOJI[vm.status]
        text += f'\n{status_emoji} {vm.name} {status_name}'
        if vm.status in (Instance.RUNNING, Instance.PROVISIONING, Instance.STARTING) \
                and vm.network_interfaces:
            net = vm.network_interfaces[0].primary_v4_address
            if net.one_to_one_nat:
                text += f' {net.one_to_one_nat.address}'
        mu.add(
            InlineKeyboardButton(vm.name, callback_data=f'vm:{vm.id}'),
        )
    bot.send_message(message.chat.id, text, reply_markup=mu)


@bot.message_handler(commands=['func'])
def handle_funcs(message):
    sdk = yandexcloud.SDK()
    functions = sdk.client(FunctionServiceStub)
    resp = functions.List(ListFunctionsRequest(folder_id=FOLDER))
    mu = InlineKeyboardMarkup()
    for func in resp.functions:
        resp_b = functions.ListAccessBindings(ListAccessBindingsRequest(resource_id=func.id))
        status_emoji = FUNCTION_STATUS_EMOJI[bool(resp_b.access_bindings)]
        mu.add(
            InlineKeyboardButton(f'{status_emoji} {func.name}', callback_data=f'func:{func.id}'),
        )
    bot.send_message(message.chat.id, 'выбери функцию', reply_markup=mu)


@bot.message_handler(commands=['cluster'])
def handle_dbs(message):
    sdk = yandexcloud.SDK()
    clusters = sdk.client(ClusterServiceStub)
    resp = clusters.List(ListClustersRequest(folder_id=FOLDER))
    mu = InlineKeyboardMarkup()
    text = "выбери кластер"
    for cluster in resp.clusters:
        status_name = Cluster.Status.Name(cluster.status)
        status_emoji = CLUSTER_STATUS_EMOJI[cluster.status]
        text += f'\n{status_emoji} {cluster.name} {status_name}'
        # TODO: строка подключения
        # получить список хостов, найти мастера
        # получить список бд
        mu.add(
            InlineKeyboardButton(cluster.name, callback_data=f'cluster:{cluster.id}'),
        )
    bot.send_message(message.chat.id, text, reply_markup=mu)


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    cmd, arg = call.data.split(':')
    if cmd == 'vm':
        vm, error = get_vm(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось получить ВМ {arg}\n\n{error}", call.message.chat.id,
                                  call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            mu = InlineKeyboardMarkup()
            mu.add(
                InlineKeyboardButton('▶ start', callback_data=f'vm-start:{arg}'),
                InlineKeyboardButton('⏹ stop', callback_data=f'vm-stop:{arg}'),
                InlineKeyboardButton('⏯ restart', callback_data=f'vm-restart:{arg}'),
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(vm.name, call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=mu)
    elif cmd == 'vm-start':
        error = start_vm(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось запустить ВМ {arg}\n\n{error}", call.message.chat.id,
                                  call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"ВМ {arg} запускается", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'vm-stop':
        error = stop_vm(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось остановить ВМ {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"ВМ {arg} останавливается", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'vm-restart':
        error = restart_vm(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось перезапустить ВМ {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"ВМ {arg} перезапускается", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'cluster':
        # получить хосты, найти мастера
        # получить список бд
        # кнопки старт / стоп
        cluster, error = get_cluster(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось получить кластер {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            mu = InlineKeyboardMarkup()
            mu.add(
                InlineKeyboardButton('▶ start', callback_data=f'cluster-start:{arg}'),
                InlineKeyboardButton('⏹ stop', callback_data=f'cluster-stop:{arg}'),
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(cluster.name, call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=mu)
    elif cmd == 'cluster-start':
        error = start_cluster(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось запустить кластер {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"кластер {arg} запускается", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'cluster-stop':
        error = stop_cluster(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось остановить кластер {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"кластер {arg} останавливается", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'func':
        func, error = get_func(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось получить функцию {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            mu = InlineKeyboardMarkup()
            mu.add(
                InlineKeyboardButton('open access', callback_data=f'func-open:{arg}'),
                InlineKeyboardButton('close access', callback_data=f'func-close:{arg}'),
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(func.name, call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=mu)
    elif cmd == 'func-open':
        error = open_func(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось открыть функцию {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"функция {arg} открыта", call.message.chat.id,
                                  call.message.message_id)
    elif cmd == 'func-close':
        error = close_func(arg)
        if error:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"не удалось закрыть функцию {arg}\n\n{error}",
                                  call.message.chat.id, call.message.message_id)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id)
            bot.edit_message_text(f"функция {arg} закрыта", call.message.chat.id,
                                  call.message.message_id)
    else:
        bot.answer_callback_query(call.id, call.data)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
        bot.send_message(call.message.chat.id, call.data)


def get_vm(vm_id: str) -> Tuple[Instance, Optional[str]]:
    sdk = yandexcloud.SDK()
    client = sdk.client(InstanceServiceStub)
    try:
        return client.Get(GetInstanceRequest(instance_id=vm_id)), None
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return None, e.details()
        return None, json.dumps(e.args)


def start_vm(vm_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(InstanceServiceStub)
    try:
        client.Start(StartInstanceRequest(instance_id=vm_id))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def stop_vm(vm_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(InstanceServiceStub)
    try:
        client.Stop(StopInstanceRequest(instance_id=vm_id))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def restart_vm(vm_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(InstanceServiceStub)
    try:
        client.Restart(RestartInstanceRequest(instance_id=vm_id))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def get_cluster(cluster_id: str) -> Tuple[Cluster, Optional[str]]:
    sdk = yandexcloud.SDK()
    client = sdk.client(ClusterServiceStub)
    try:
        return client.Get(GetClusterRequest(cluster_id=cluster_id)), None
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return None, e.details()
        return None, json.dumps(e.args)


def start_cluster(cluster_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(ClusterServiceStub)
    try:
        client.Start(StartClusterRequest(cluster_id=cluster_id))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def stop_cluster(cluster_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(ClusterServiceStub)
    try:
        client.Stop(StopClusterRequest(cluster_id=cluster_id))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def get_func(func_id: str) -> Tuple[Function, Optional[str]]:
    sdk = yandexcloud.SDK()
    client = sdk.client(FunctionServiceStub)
    try:
        return client.Get(GetFunctionRequest(function_id=func_id)), None
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return None, e.details()
        return None, json.dumps(e.args)


def open_func(func_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(FunctionServiceStub)
    try:
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
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)


def close_func(func_id: str) -> Optional[str]:
    sdk = yandexcloud.SDK()
    client = sdk.client(FunctionServiceStub)
    try:
        client.SetAccessBindings(SetAccessBindingsRequest(
            resource_id=func_id,
            access_bindings=[],
        ))
    except grpc.RpcError as e:
        if hasattr(e, 'details'):
            return e.details()
        return json.dumps(e.args)
