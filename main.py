#!/usr/bin/env python

import inspect
import logging
import os
import re
import sys
from functools import partial
from typing import (
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import telebot
import yandexcloud
from telebot.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from commands import ReplyFunc, commands

# todo: validate env
# todo: .env file
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLOUD_TOKEN = os.getenv('CLOUD_TOKEN')
FOLDER = os.getenv('FOLDER')
DEBUG_LIB = os.getenv('DEBUG_LIB')
USERS_WHITELIST = os.getenv('TG_USERS_WHITELIST', '').split(';')

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='markdown')  # some escaping problem. MarkdownV2 same

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

if DEBUG_LIB:
    logging.getLogger('TeleBot').setLevel(logging.DEBUG)

command_regex = re.compile('/(?P<cmd>[^ ]+)(?P<args>.*)')


class UsersWhiteList(telebot.custom_filters.SimpleCustomFilter):
    key = 'whitelist'

    def check(self, event: Union[Message, CallbackQuery]):
        if isinstance(event, CallbackQuery):
            message = event.message
        else:
            message = event
        sender = event.from_user.username
        if sender not in USERS_WHITELIST:
            logger.warning('rejected message from user %r: %r', sender, message.text)
            return False
        return True


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


def ss_entry(event: dict, _context):
    update = Update.de_json(event['body'])
    logger.debug('%r', update)
    bot.process_new_updates([update])
    return {
        'statusCode': 200,
    }


@bot.middleware_handler(update_types=['message', 'callback_query'])
def log_messages(_bot_instance: telebot.TeleBot, event: Union[Message, CallbackQuery]):
    if isinstance(event, CallbackQuery):
        logger.info('[%s] callback from user %r: %r',
                    event.id, event.from_user.username, event.data)
    else:
        logger.info('[%s] message from user %r: %r', event.id, event.from_user.username, event.text)


@bot.message_handler(whitelist=True, commands=['start', 'help'])
def handle_help(message: Message):
    docs = [command.build_help() for command in commands.values()]
    doc = '**available commands**:\n\n/help - show this help\n\n' + '\n\n'.join(docs)
    bot.send_message(message.chat.id, doc)


@bot.message_handler(whitelist=True, commands=list(commands.keys()))
def handle_commands(message: Message):
    reply = cast(ReplyFunc, partial(_reply, message.chat.id, None))
    process_command(reply, message.text, str(message.id))


@bot.callback_query_handler(whitelist=True, func=lambda call: True)
def handle_callback_query(call: CallbackQuery):
    reply = cast(ReplyFunc, partial(_reply, call.message.chat.id, call.message.id))
    process_command(reply, call.data, str(call.id))
    bot.answer_callback_query(call.id)


def process_command(reply: ReplyFunc, command_text: str, request_id: str) -> None:
    command_name, args = parse_command_args(command_text)
    logger.info('[%s] command %r, args %s', request_id, command_name, args)
    command_class = commands[command_name]
    sdk = yandexcloud.SDK(token=CLOUD_TOKEN)
    repo_class = inspect.signature(command_class.__init__).parameters['repo'].annotation
    client_stub = inspect.signature(repo_class.__init__).parameters['client'].annotation
    client = sdk.client(client_stub)
    repo = repo_class(FOLDER, client)
    command = command_class(args, request_id, reply, repo)
    command.run()


def parse_command_args(text: str) -> Tuple[str, List[str]]:
    match = command_regex.match(text).groupdict()
    cmd = match['cmd']
    args = match.get('args', '').split()
    return cmd, args


if __name__ == '__main__':
    bot.add_custom_filter(UsersWhiteList())
    bot.set_my_commands(
        [
            BotCommand('help', 'show help'),
        ] + [
            BotCommand(command.name, command.short_doc())
            for command in commands.values()
        ]
    )
    bot.remove_webhook()
    bot.polling()
