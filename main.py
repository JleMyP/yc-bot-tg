#!/usr/bin/env python

import logging
import os
import re
from functools import partial
from typing import (
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    cast,
)

import telebot
import yandexcloud
from emoji import emojize
from telebot.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from yandex.cloud.compute.v1.instance_pb2 import Instance
from yandex.cloud.mdb.postgresql.v1.cluster_pb2 import Cluster

import cloud_api

# TODO:
#   improve errors handling (sentry?)
#   git
#     auto deploy
#   logging
#   per-command help
#   separate startup scenarios
#     set / remove hook
#     setup commands
#   improve vm management
#     make ip static / dynamic
#   add instance group management
#     on / off
#   add access control
#     online veryfi
#   refactor
#     decouple from a cloud specific
#   env file support

# todo: validate env


BOT_TOKEN = os.getenv('BOT_TOKEN')
CLOUD_TOKEN = os.getenv('CLOUD_TOKEN')
FOLDER = os.getenv('FOLDER')
DEBUG_LIB = os.getenv('DEBUG_LIB')
USERS_WHITELIST = os.getenv('TG_USERS_WHITELIST', '').split(';')

INSTANCE_STATUS_EMOJI = {
    Instance.PROVISIONING: emojize(':blue_circle:'),
    Instance.RUNNING: emojize(':green_circle:'),
    Instance.STOPPING: emojize(':red_circle:'),
    Instance.STOPPED: emojize(':red_circle:'),
    Instance.STARTING: emojize(':blue_circle:'),
    Instance.RESTARTING: emojize(':blue_circle:'),
    Instance.UPDATING: emojize(':blue_circle:'),
    Instance.ERROR: emojize(':red_exclamation_mark:'),
    Instance.CRASHED: emojize(':red_exclamation_mark:'),
    Instance.DELETING: emojize(':wastebasket:'),
}
CLUSTER_STATUS_EMOJI = {
    Cluster.STATUS_UNKNOWN: emojize(':white_question_mark:'),
    Cluster.CREATING: emojize(':blue_circle:'),
    Cluster.RUNNING: emojize(':green_circle:'),
    Cluster.ERROR: emojize(':red_exclamation_mark:'),
    Cluster.UPDATING: emojize(':blue_circle:'),
    Cluster.STOPPING: emojize(':red_circle:'),
    Cluster.STOPPED: emojize(':red_circle:'),
    Cluster.STARTING: emojize(':blue_circle:'),
}
FUNCTION_STATUS_EMOJI = {
    True: emojize(':green_circle:'),
    False: emojize(':red_circle:'),
}

HELP = '''
Available commands:

/start, /help - show this help.

/vm - compute instances management
`/vm [list]` - show instances with their status and public ip;
`/vm [get] <id>` - show actions for specified instance;
`/vm [start | stop | restart] <id>` - do specified action on instance.

/pg - postgresql clusters management
`/pg [list]` - show instances with theirs status;
`/pg [get] <id>` - show actions for specified cluster;
`/pg [start | stop] <id>` - do specified action on cluster.

/func - serverless functions management
`/func [list]` - show functions with their public access status (opened / closed);
`/func [get] <id>` - show actions for specified function;
`/func open <id>` - allow public access to invoke function;
`/func close <id>` - disallow public access to invoke function.
'''

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='markdown')
sdk = yandexcloud.SDK(token=CLOUD_TOKEN)
logger = logging.getLogger(__name__)

if DEBUG_LIB:
    logging.getLogger('TeleBot').setLevel(logging.DEBUG)

command_regex = re.compile('/(?P<cmd>[^ ]+)(?P<args>.*)')


class ReplyFunc(Protocol):
    def __call__(
        self,
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
        *,
        edit: bool,
    ) -> None:
        ...


class UsersWhiteList(telebot.custom_filters.SimpleCustomFilter):
    key = 'whitelist'

    def check(self, event: Union[Message, CallbackQuery]):
        if event is CallbackQuery:
            message = event.message
        else:
            message = event
        sender = message.from_user.username
        if sender not in USERS_WHITELIST:
            logger.warning('rejected message from user `%s`: `%s`', sender, message.text)
            return False
        return True


bot.add_custom_filter(UsersWhiteList())


def _reply(
        chat_id: int,
        message_id: Optional[int],
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
        *,
        edit: bool,
) -> None:
    if message_id and edit:
        # throws error if content (text, keyboard) is not changed
        bot.edit_message_text(text, chat_id, message_id)
        if markup:
            bot.edit_message_reply_markup(chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


def handler(event: dict, _context):
    update = Update.de_json(event['body'])
    logger.debug('%r', update)
    bot.process_new_updates([update])
    return {
        'statusCode': 200,
    }


@bot.message_handler(whitelist=True, commands=['start', 'help'])
def handle_help(message: Message):
    bot.send_message(message.chat.id, HELP)


@bot.message_handler(whitelist=True, commands=['test'])
def handle_test(message: Message):
    mu = InlineKeyboardMarkup()
    mu.add(
        InlineKeyboardButton('button 1', callback_data='1:1'),
        InlineKeyboardButton('button 2', callback_data='2:1'),
    )
    bot.send_message(message.chat.id, 'message', reply_markup=mu)


@bot.message_handler(whitelist=True, commands=['vm', 'pg', 'func'])
def handle_commands(message: Message):
    command, args = parse_command_args(message.text)
    handlers = resource_handlers[command]

    if not args:
        action = 'list'
    elif len(args) == 1 and args[0] not in handlers.keys():
        action = 'get'
    else:
        action, *args = args
        if action not in handlers.keys():
            bot.send_message(message.chat.id, 'wrong action')
            return

    if action == 'list':
        args = [FOLDER]

    command_handler = handlers[action]
    # todo: check args count
    reply = partial(_reply, message.chat.id, None)
    command_handler(reply, *args)


@bot.callback_query_handler(whitelist=True, func=lambda call: True)
def handle_callback_query(call: CallbackQuery):
    resource, cmd, arg = call.data.split(':')
    resource_handler = resource_handlers.get(resource, {}).get(cmd)
    reply = cast(ReplyFunc, partial(_reply, call.message.chat.id, call.message.id))
    if resource_handler:
        resource_handler(reply, arg)
    else:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.id)
    bot.answer_callback_query(call.id)


def parse_command_args(text: str) -> Tuple[str, List[str]]:
    match = command_regex.match(text).groupdict()
    cmd = match['cmd']
    args = match.get('args', '').split()
    return cmd, args


def vm_list_handler(reply: ReplyFunc, folder_id: str) -> None:
    instances, error = cloud_api.vm_list(folder_id)
    if error:
        reply(f'unable to get vm list\n\n{error}', edit=False)
        return

    markup = InlineKeyboardMarkup()
    text = 'choose vm'
    for vm in instances:
        status_name = Instance.Status.Name(vm.status)
        status_emoji = INSTANCE_STATUS_EMOJI[vm.status]
        text += f'\n{status_emoji} {vm.name} {status_name}'
        if vm.status in (Instance.RUNNING, Instance.PROVISIONING, Instance.STARTING):
            for net in vm.network_interfaces:
                if net.primary_v4_address.one_to_one_nat:
                    text += f' {net.primary_v4_address.one_to_one_nat.address}'
        markup.add(
            InlineKeyboardButton(vm.name, callback_data=f'vm:get:{vm.id}'),
        )
    reply(text, markup, edit=False)


def vm_get_handler(reply: ReplyFunc, vm_id: str) -> None:
    vm, error = cloud_api.vm_get(vm_id)
    if error:
        reply(f'unable to get vm `{vm_id}`\n\n{error}', edit=True)
        return

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton('▶ start', callback_data=f'vm:start:{vm_id}'),
        InlineKeyboardButton('⏹ stop', callback_data=f'vm:stop:{vm_id}'),
        InlineKeyboardButton('⏯ restart', callback_data=f'vm:restart:{vm_id}'),
    ]])
    reply(vm.name, markup, edit=True)


def vm_start_handler(reply: ReplyFunc, vm_id: str) -> None:
    error = cloud_api.vm_start(vm_id)
    if error:
        reply(f'unable to start vm `{vm_id}`\n\n{error}', edit=True)
    else:
        reply(f'vm `{vm_id}` starting', edit=True)


def vm_stop_handler(reply: ReplyFunc, vm_id: str) -> None:
    error = cloud_api.vm_stop(vm_id)
    if error:
        reply(f'unable to stop vm `{vm_id}`\n\n{error}', edit=True)
    else:
        reply(f'vm `{vm_id}` stopping', edit=True)


def vm_restart_handler(reply: ReplyFunc, vm_id: str) -> None:
    error = cloud_api.vm_restart(vm_id)
    if error:
        reply(f'unable to restart vm `{vm_id}`\n\n{error}', edit=True)
    else:
        reply(f'vm `{vm_id}` restarting', edit=True)


def pg_list_handler(reply: ReplyFunc, folder_id: str) -> None:
    clusters, error = cloud_api.pg_list(folder_id)
    if error:
        reply(f'unable to get pg clusters list\n\n{error}', edit=False)
        return

    markup = InlineKeyboardMarkup()
    text = 'choose pg cluster'
    for cluster in clusters:
        status_name = Cluster.Status.Name(cluster.status)
        status_emoji = CLUSTER_STATUS_EMOJI[cluster.status]
        text += f'\n{status_emoji} {cluster.name} {status_name}'
        # TODO: connection string
        markup.add(
            InlineKeyboardButton(cluster.name, callback_data=f'pg:get:{cluster.id}'),
        )
    reply(text, markup, edit=False)


def pg_get_handler(reply: ReplyFunc, pg_id: str) -> None:
    cluster, error = cloud_api.pg_get(pg_id)
    if error:
        reply(f'unable to get pg cluster `{pg_id}`\n\n{error}', edit=True)
    else:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton('▶ start', callback_data=f'pg:start:{pg_id}'),
            InlineKeyboardButton('⏹ stop', callback_data=f'pg:stop:{pg_id}'),
        ]])
        reply(cluster.name, markup, edit=True)


def pg_start_handler(reply: ReplyFunc, pg_id: str) -> None:
    error = cloud_api.pg_start(pg_id)
    if error:
        reply(f'unable to start pg cluster `{pg_id}`\n\n{error}', edit=True)
    else:
        reply(f'pg cluster `{pg_id}` starting', edit=True)


def pg_stop_handler(reply: ReplyFunc, pg_id: str) -> None:
    error = cloud_api.pg_stop(pg_id)
    if error:
        reply(f'unable to stop pg cluster `{pg_id}`\n\n{error}', edit=True)
    else:
        reply(f'pg cluster `{pg_id}` stopping', edit=True)


def func_list_handler(reply: ReplyFunc, folder_id: str) -> None:
    functions, error = cloud_api.func_list(folder_id)
    if error:
        reply(f'unable to get functions list\n\n{error}', edit=False)
        return

    markup = InlineKeyboardMarkup()
    for func, is_opened in functions:
        status_emoji = FUNCTION_STATUS_EMOJI[is_opened]
        markup.add(
            InlineKeyboardButton(
                f'{status_emoji} {func.name}',
                callback_data=f'func:get:{func.id}',
            ),
        )

    reply('choose function', markup, edit=False)


def func_get_handler(reply: ReplyFunc, func_id: str) -> None:
    func, error = cloud_api.func_get(func_id)
    if error:
        reply(f'unable to get func `{func_id}`\n\n{error}', edit=True)
    else:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton('open access', callback_data=f'func:open:{func_id}'),
            InlineKeyboardButton('close access', callback_data=f'func:close:{func_id}'),
        ]])
        reply(func.name, markup, edit=True)


def func_open_handler(reply: ReplyFunc, func_id: str) -> None:
    error = cloud_api.func_open(func_id)
    if error:
        reply(f'unable to open func `{func_id}`\n\n{error}', edit=True)
    else:
        reply(f'func `{func_id}` opened', edit=True)


def func_close_handler(reply: ReplyFunc, func_id: str) -> None:
    error = cloud_api.func_close(func_id)
    if error:
        reply(f'unable to close func `{func_id}\n\n{error}`', edit=True)
    else:
        reply(f'func `{func_id}` closed', edit=True)


resource_handlers = {
    'vm': {
        'list': vm_list_handler,
        'get': vm_get_handler,
        'start': vm_start_handler,
        'stop': vm_stop_handler,
        'restart': vm_restart_handler,
    },
    'pg': {
        'list': pg_list_handler,
        'get': pg_get_handler,
        'start': pg_start_handler,
        'stop': pg_stop_handler,
    },
    'func': {
        'list': func_list_handler,
        'get': func_get_handler,
        'open': func_open_handler,
        'close': func_close_handler,
    },
}


if __name__ == '__main__':
    bot.set_my_commands([
        BotCommand('help', 'show help'),
        BotCommand('vm', 'manage compute instances'),
        BotCommand('pg', 'manage pg clusters'),
        BotCommand('func', 'manage serverless functions'),
    ])
    bot.remove_webhook()
    bot.polling()
