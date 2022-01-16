import inspect
import logging
import re
from functools import partial
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    cast,
)

from emoji import emojize
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_api import (
    AppException,
    Instance,
    InstanceRepo,
    InstanceStatus,
    PostgresCluster,
    PostgresClusterStatus,
    PostgresRepo,
    ServerlessFunction,
    ServerlessFunctionRepo,
    ServerlessFunctionStatus,
)

logger = logging.getLogger(__name__)


class ReplyFunc(Protocol):
    def __call__(
        self,
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None,
        *,
        edit: bool,
    ) -> None:
        ...


class BoundReplyFunc(Protocol):
    def __call__(self, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
        ...


class BaseCommand:
    _def_regex = re.compile('def (.+)[(]')
    _sub_commands: Dict[str, Tuple[Callable, list]]  # name: (func, args)
    _default_command: Optional[str]
    _optional_command: Optional[str]

    name: str

    def __init__(self, args: List[str], request_id: str, reply: ReplyFunc) -> None:
        self._args = args
        self._request_id = request_id
        self._reply_raw = reply
        self._reply = cast(BoundReplyFunc, partial(reply, edit=False))
        self._reply_inline = cast(BoundReplyFunc, partial(reply, edit=True))

    def __init_subclass__(cls):
        super().__init_subclass__()
        cls._sub_commands = {'help': (cls.help, [])}
        cls._default_command = cls._optional_command = None

        methods = cls._def_regex.findall(inspect.getsource(cls))
        members = [pair for pair in inspect.getmembers(cls) if hasattr(pair[1], '_is_sub_command')]
        members.sort(key=lambda pair: methods.index(pair[0]))

        for name, method in members:
            cls._sub_commands[name] = (method, getattr(method, '_args'))  # noqa: B009
            if getattr(method, '_is_default', False):
                cls._default_command = name
            elif getattr(method, '_is_optional', False):
                cls._optional_command = name

    @classmethod
    def register(cls, *, default: bool = False, optional: bool = False):
        def decorator(method: Callable) -> Callable:
            args = []
            spec = [f'[{method.__name__}]' if optional or default else method.__name__]
            signature = inspect.signature(method)
            for parameter in list(signature.parameters.values())[1:]:
                if parameter.name.startswith('_'):
                    continue

                parameter_type = None
                if parameter.annotation is not parameter.empty:
                    parameter_type = parameter.annotation.__name__
                arg = {
                    'name': parameter.name.rstrip('_'),
                    'type': parameter_type,
                    'optional': parameter.default is not parameter.empty,
                }
                args.append(arg)

                arg_spec = arg['name']
                if arg['type']:
                    arg_spec += f':{arg["type"]}'
                arg_spec = f'[{arg_spec}]' if arg['optional'] else f'<{arg_spec}>'
                spec.append(arg_spec)

            method._args = args
            method._formatted_spec = ' '.join(spec)
            method._is_sub_command = True
            method._is_default = default
            method._is_optional = optional
            return method
        return decorator

    @classmethod
    def format_command(cls, *args) -> str:
        text = f'/{cls.name}'
        if args:
            text += ' ' + ' '.join(map(str, args))
        return text

    @classmethod
    def short_doc(cls) -> str:
        return inspect.getdoc(cls)

    @classmethod
    def build_help(cls) -> str:
        docs = [f'/{cls.name} - {inspect.getdoc(cls)}\n']
        for func, _ in cls._sub_commands.values():
            spec = getattr(func, '_formatted_spec', func.__name__)
            docs.append(f'`/{cls.name} {spec}` - {inspect.getdoc(func)}')
        return emojize('\n'.join(docs))

    def help(self):  # noqa: A003
        """
        show commands
        """
        help_text = self.build_help()
        self._reply(help_text)

    def _reply_error(self, text: str, *, inline: bool = False) -> None:
        logger.warning('[%s] error: %s', self._request_id, text)
        text = emojize(f':warning: `{text}`')
        self._reply_raw(text, edit=inline)

    def run(self):
        if not self._args:
            action = self._default_command
            args = []
        elif len(self._args) == 1 and self._args[0] not in self._sub_commands:
            action = self._optional_command
            args = self._args[:]
        else:
            action, *args = self._args
            if action not in self._sub_commands:
                self._reply_error('wrong action')
                return

        if not action:
            self._reply_error('action required')
            return

        handler, handler_args = self._sub_commands[action]
        required_handler_args = [arg for arg in handler_args if not arg['optional']]
        if len(args) < len(required_handler_args) or len(args) > len(handler_args):
            self._reply_error(f'wrong args count (expected {len(required_handler_args)})')
            return

        logger.info('[%s] action %r, args %s', self._request_id, action, args)

        try:
            handler(self, *args)
        except AppException as err:
            self._reply_error(err.format())


class InstanceCommand(BaseCommand):
    """
    compute instances management
    """
    name = 'vm'

    _STATUS_EMOJI = {
        InstanceStatus.RUNNING: emojize(':green_circle:'),
        InstanceStatus.STOPPED: emojize(':red_circle:'),
        InstanceStatus.IN_PROGRESS: emojize(':blue_circle:'),
        InstanceStatus.ERROR: emojize(':red_exclamation_mark:'),
    }

    def __init__(self, *args, repo: InstanceRepo, **kwargs):
        super().__init__(*args, **kwargs)
        self._repo = repo

    def _format_instance(self, instance: Instance) -> str:
        status_emoji = self._STATUS_EMOJI[instance.status]
        text = f'{status_emoji} {instance.name} {instance.status.value}'
        if instance.public_ips:
            text += ' ' + ' '.join(instance.public_ips)
        return text

    @BaseCommand.register(default=True)
    def list(self, inline: bool = False) -> None:  # noqa: A003
        """
        show instances with their status and public ip
        """
        instances = self._repo.get_list()
        markup = InlineKeyboardMarkup()
        text = ['choose vm']
        for instance in instances:
            text.append(self._format_instance(instance))
            markup.add(
                InlineKeyboardButton(instance.name,
                                     callback_data=self.format_command('get', instance.id)),
            )
        markup.add(InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                        callback_data=self.format_command('list', 'true')))
        if inline:
            self._reply_inline('\n'.join(text), markup)
        else:
            self._reply('\n'.join(text), markup)

    @BaseCommand.register(optional=True)
    def get(self, id_or_name: str) -> None:
        """
        show actions for specified instance
        """
        instance = self._repo.get_single(id_or_name)
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(emojize(':play_button: start'),
                                     callback_data=self.format_command('start', instance.id)),
                InlineKeyboardButton(emojize(':stop_button: stop'),
                                     callback_data=self.format_command('stop', instance.id)),
                InlineKeyboardButton(emojize(':repeat_button: restart'),
                                     callback_data=self.format_command('restart', instance.id)),
            ],
            [
                InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                     callback_data=self.format_command('get', instance.id)),
            ],
        ])
        self._reply_inline(self._format_instance(instance), markup)

    @BaseCommand.register()
    def start(self, id_or_name: str) -> None:
        """
        start the instance
        """
        instance = self._repo.get_single(id_or_name)
        self._repo.start(instance.id)
        # todo: "wait" button
        self._reply_inline(f'instance `{instance.name}` starting')

    @BaseCommand.register()
    def stop(self, id_or_name: str) -> None:
        """
        stop the instance
        """
        instance = self._repo.get_single(id_or_name)
        self._repo.stop(instance.id)
        # todo: "wait" button
        self._reply_inline(f'instance `{instance.name}` stopping')

    @BaseCommand.register()
    def restart(self, id_or_name: str) -> None:
        """
        restart the instance
        """
        instance = self._repo.get_single(id_or_name)
        self._repo.restart(instance.id)
        # todo: "wait" button
        self._reply_inline(f'instance `{instance.name}` restarting')


class PostgresCommand(BaseCommand):
    """
    postgres clusters management
    """
    name = 'pg'

    _STATUS_EMOJI = {
        PostgresClusterStatus.UNKNOWN: emojize(':white_question_mark:'),
        PostgresClusterStatus.IN_PROGRESS: emojize(':blue_circle:'),
        PostgresClusterStatus.RUNNING: emojize(':green_circle:'),
        PostgresClusterStatus.ERROR: emojize(':red_exclamation_mark:'),
        PostgresClusterStatus.STOPPED: emojize(':red_circle:'),
    }

    def __init__(self, *args, repo: PostgresRepo, **kwargs):
        super().__init__(*args, **kwargs)
        self._repo = repo

    def _format_pg(self, pg: PostgresCluster) -> str:
        status_emoji = self._STATUS_EMOJI[pg.status]
        # TODO: connection string
        return f'{status_emoji} {pg.name} {pg.status.name}'

    @BaseCommand.register(default=True)
    def list(self, inline: bool = False) -> None:  # noqa: A003
        """
        show pg clusters with their status
        """
        clusters = self._repo.get_list()
        markup = InlineKeyboardMarkup()
        text = ['choose pg cluster']
        for cluster in clusters:
            text.append(self._format_pg(cluster))
            markup.add(
                InlineKeyboardButton(cluster.name,
                                     callback_data=self.format_command('get', cluster.id)),
            )
        markup.add(InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                        callback_data=self.format_command('list', 'true')))
        if inline:
            self._reply_inline('\n'.join(text), markup)
        else:
            self._reply('\n'.join(text), markup)

    @BaseCommand.register(optional=True)
    def get(self, id_or_name: str) -> None:
        """
        show actions for specified pg cluster
        """
        pg = self._repo.get_single(id_or_name)
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(emojize(':play_button: start'),
                                     callback_data=self.format_command('start', pg.id)),
                InlineKeyboardButton(emojize(':stop_button: stop'),
                                     callback_data=self.format_command('stop', pg.id)),
            ],
            [
                InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                     callback_data=self.format_command('get', pg.id))
            ],
        ])
        self._reply_inline(self._format_pg(pg), markup)

    @BaseCommand.register()
    def start(self, id_or_name: str) -> None:
        """
        start the cluster
        """
        pg = self._repo.get_single(id_or_name)
        self._repo.start(pg.id)
        self._reply_inline(f'pg cluster `{pg.name}` starting')

    @BaseCommand.register()
    def stop(self, id_or_name: str) -> None:
        """
        stop the cluster
        """
        pg = self._repo.get_single(id_or_name)
        self._repo.stop(pg.id)
        self._reply_inline(f'pg cluster `{pg.name}` stopping')


class FunctionCommand(BaseCommand):
    """
    serverless functions management
    """
    name = 'func'

    _STATUS_EMOJI = {
        ServerlessFunctionStatus.OPENED: emojize(':green_circle:'),
        ServerlessFunctionStatus.CLOSED: emojize(':prohibited:'),
    }

    def __init__(self, *args, repo: ServerlessFunctionRepo, **kwargs):
        super().__init__(*args, **kwargs)
        self._repo = repo

    def _format_func(self, function: ServerlessFunction) -> str:
        status_emoji = self._STATUS_EMOJI[function.status]
        return f'{status_emoji} {function.name} [invoke]({function.invoke_url})'

    @BaseCommand.register(default=True)
    def list(self, inline: bool = False) -> None:  # noqa: A003
        """
        show functions with their public access status (opened / closed)
        """
        functions = self._repo.get_list()
        markup = InlineKeyboardMarkup()
        text = ['choose function']
        for function in functions:
            text.append(self._format_func(function))
            markup.add(
                InlineKeyboardButton(function.name,
                                     callback_data=self.format_command('get', function.id)),
            )
        markup.add(InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                        callback_data=self.format_command('list', 'true')))
        if inline:
            self._reply_inline('\n'.join(text), markup)
        else:
            self._reply('\n'.join(text), markup)

    @BaseCommand.register(optional=True)
    def get(self, id_or_name: str) -> None:
        """
        show actions for specified function
        """
        function = self._repo.get_single(id_or_name)
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(emojize(':play_button: open'),
                                     callback_data=self.format_command('open', function.id)),
                InlineKeyboardButton(emojize(':stop_button: close'),
                                     callback_data=self.format_command('close', function.id)),
            ],
            [
                InlineKeyboardButton(emojize(':recycling_symbol: refresh'),
                                     callback_data=self.format_command('get', function.id))
            ],
        ])
        self._reply_inline(self._format_func(function), markup)

    @BaseCommand.register()
    def open(self, id_or_name: str) -> None:  # noqa: A003
        """
        allow public access to invoke function
        """
        function = self._repo.get_single(id_or_name)
        self._repo.open(function.id)
        self._reply_inline(f'function `{function.name}` opened')

    @BaseCommand.register()
    def close(self, id_or_name: str) -> None:
        """
        disallow public access to invoke function
        """
        function = self._repo.get_single(id_or_name)
        self._repo.close(function.id)
        self._reply_inline(f'function `{function.name}` closed')


commands = {cls.name: cls for cls in BaseCommand.__subclasses__()}
