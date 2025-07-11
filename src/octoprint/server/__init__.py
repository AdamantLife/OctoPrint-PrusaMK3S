__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import atexit
import base64
import functools
import logging
import logging.config
import mimetypes
import os
import pathlib
import re
import signal
import sys
import time
import uuid  # noqa: F401
from collections import OrderedDict, defaultdict

from babel import Locale
from flask import (  # noqa: F401
    Blueprint,
    Flask,
    Request,
    Response,
    current_app,
    g,
    make_response,
    request,
    session,
)
from flask_assets import Bundle, Environment
from flask_babel import Babel, gettext, ngettext  # noqa: F401
from flask_login import (  # noqa: F401
    LoginManager,
    current_user,
    login_user,
    session_protected,
    user_loaded_from_cookie,
    user_logged_out,
)
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from werkzeug.exceptions import HTTPException

import octoprint.events
import octoprint.filemanager
import octoprint.util
import octoprint.util.net
from octoprint.server import util
from octoprint.systemcommands import system_command_manager
from octoprint.util.json import JsonEncoding
from octoprint.vendor.flask_principal import (  # noqa: F401
    AnonymousIdentity,
    Identity,
    Permission,
    Principal,
    RoleNeed,
    UserNeed,
    identity_changed,
    identity_loaded,
)
from octoprint.vendor.sockjs.tornado import SockJSRouter

try:
    import fcntl
except ImportError:
    fcntl = None

SUCCESS = {}
NO_CONTENT = ("", 204, {"Content-Type": "text/plain"})
NOT_MODIFIED = ("Not Modified", 304, {"Content-Type": "text/plain"})

app = Flask("octoprint")

assets = None
babel = None
limiter = None
debug = False
safe_mode = False

printer = None
printerProfileManager = None
fileManager = None
slicingManager = None
analysisQueue = None
userManager = None
permissionManager = None
groupManager = None
eventManager = None
loginManager = None
pluginManager = None
pluginLifecycleManager = None
preemptiveCache = None
jsonEncoder = None
jsonDecoder = None
connectivityChecker = None
environmentDetector = None


class OctoPrintAnonymousIdentity(AnonymousIdentity):
    def __init__(self):
        super().__init__()

        user = userManager.anonymous_user_factory()

        self.provides.add(UserNeed(user.get_id()))
        for need in user.needs:
            self.provides.add(need)


import octoprint.access.groups as groups  # noqa: E402
import octoprint.access.permissions as permissions  # noqa: E402

# we set admin_permission to a GroupPermission with the default admin group
admin_permission = octoprint.util.variable_deprecated(
    "admin_permission has been deprecated, please use individual Permissions instead",
    since="1.4.0",
)(groups.GroupPermission(groups.ADMIN_GROUP))

# we set user_permission to a GroupPermission with the default user group
user_permission = octoprint.util.variable_deprecated(
    "user_permission has been deprecated, please use individual Permissions instead",
    since="1.4.0",
)(groups.GroupPermission(groups.USER_GROUP))

import octoprint._version  # noqa: E402
import octoprint.access.groups as groups  # noqa: E402
import octoprint.access.users as users  # noqa: E402
import octoprint.events as events  # noqa: E402
import octoprint.filemanager.analysis  # noqa: E402
import octoprint.filemanager.storage  # noqa: E402
import octoprint.plugin  # noqa: E402
import octoprint.slicing  # noqa: E402
import octoprint.timelapse  # noqa: E402

# only import further octoprint stuff down here, as it might depend on things defined above to be initialized already
from octoprint import __branch__, __display_version__, __revision__, __version__
from octoprint.printer.profile import PrinterProfileManager
from octoprint.printer.standard import Printer
from octoprint.server.util import (
    corsRequestHandler,
    corsResponseHandler,
    csrfRequestHandler,
    requireLoginRequestHandler,
)
from octoprint.server.util.flask import PreemptiveCache, validate_session_signature
from octoprint.settings import settings

VERSION = __version__
BRANCH = __branch__
DISPLAY_VERSION = __display_version__
REVISION = __revision__

LOCALES = []
LANGUAGES = set()


@identity_loaded.connect_via(app)
def on_identity_loaded(sender, identity):
    user = load_user(identity.id)
    if user is None:
        user = userManager.anonymous_user_factory()

    identity.provides.add(UserNeed(user.get_id()))
    for need in user.needs:
        identity.provides.add(need)


def _clear_identity(sender):
    # Remove session keys set by Flask-Principal
    for key in ("identity.id", "identity.name", "identity.auth_type"):
        session.pop(key, None)

    # switch to anonymous identity
    identity_changed.send(sender, identity=AnonymousIdentity())


@session_protected.connect_via(app)
def on_session_protected(sender):
    # session was deleted by session protection, that means the user is no more and we need to clear our identity
    if session.get("remember", None) == "clear":
        _clear_identity(sender)


@user_logged_out.connect_via(app)
def on_user_logged_out(sender, user=None):
    # user was logged out, clear identity
    _clear_identity(sender)


@user_loaded_from_cookie.connect_via(app)
def on_user_loaded_from_cookie(sender, user=None):
    if user:
        session["login_mechanism"] = util.LoginMechanism.REMEMBER_ME
        session["credentials_seen"] = False


def load_user(id):
    if id is None:
        return None

    if id == "_api":
        return userManager.api_user_factory()

    if session and "usersession.id" in session:
        sessionid = session["usersession.id"]
    else:
        sessionid = None

    if session and "usersession.signature" in session:
        sessionsig = session["usersession.signature"]
    else:
        sessionsig = ""

    if sessionid:
        # session["_fresh"] is False if the session comes from a remember me cookie,
        # True if it came from a use of the login dialog
        user = userManager.find_user(
            userid=id, session=sessionid, fresh=session.get("_fresh", False)
        )
    else:
        user = userManager.find_user(userid=id)

    if (
        user
        and user.is_active
        and (not sessionid or validate_session_signature(sessionsig, id, sessionid))
    ):
        return user

    return None


def load_user_from_request(request):
    # API key?
    apikey = util.get_api_key(request)
    if apikey:
        user = util.get_user_for_apikey(apikey)
        if user:
            return user

    if settings().getBoolean(["accessControl", "trustBasicAuthentication"]):
        # Basic Authentication?
        user = util.get_user_for_authorization_header(request)
        if user:
            return user

    if settings().getBoolean(["accessControl", "trustRemoteUser"]):
        # Remote user header?
        user = util.get_user_for_remote_user_header(request)
        if user:
            return user

    return None


def unauthorized_user():
    from flask import abort

    abort(403)


# ~~ startup code


class Server:
    def __init__(
        self,
        settings=None,
        plugin_manager=None,
        connectivity_checker=None,
        environment_detector=None,
        event_manager=None,
        host=None,
        port=None,
        v6_only=False,
        debug=False,
        safe_mode=False,
        allow_root=False,
        octoprint_daemon=None,
    ):
        self._settings = settings
        self._plugin_manager = plugin_manager
        self._connectivity_checker = connectivity_checker
        self._environment_detector = environment_detector
        self._event_manager = event_manager
        self._host = host
        self._port = port
        self._v6_only = v6_only
        self._debug = debug
        self._safe_mode = safe_mode
        self._allow_root = allow_root
        self._octoprint_daemon = octoprint_daemon

        self._logger = logging.getLogger(__name__)
        self._setup_heartbeat_logging()

        self._lifecycle_callbacks = defaultdict(list)

        self._intermediary_server = None
        self._server = None
        self._watched_observer = None

        if not self._allow_root:
            self._check_for_root()

        if self._settings is None:
            self._settings = settings()

        if self._plugin_manager is None:
            self._plugin_manager = octoprint.plugin.plugin_manager()

        if self._settings.getBoolean(["serial", "log"]):
            # enable debug logging to serial.log
            logging.getLogger("SERIAL").setLevel(logging.DEBUG)

        if self._settings.getBoolean(["devel", "pluginTimings"]):
            # enable plugin timings log
            logging.getLogger("PLUGIN_TIMINGS").setLevel(logging.DEBUG)

    def run(self):
        incomplete_startup_flag = self._get_incomplete_startup_flag()
        if not self._settings.getBoolean(["server", "ignoreIncompleteStartup"]):
            try:
                incomplete_startup_flag.touch()
            except Exception:
                self._logger.exception("Could not create startup triggered safemode flag")

        global app
        global babel

        global printer
        global printerProfileManager
        global fileManager
        global slicingManager
        global analysisQueue
        global userManager
        global permissionManager
        global groupManager
        global eventManager
        global loginManager
        global pluginManager
        global pluginLifecycleManager
        global preemptiveCache
        global jsonEncoder
        global jsonDecoder
        global connectivityChecker
        global environmentDetector
        global debug
        global safe_mode

        pluginManager = self._plugin_manager
        debug = self._debug
        safe_mode = self._safe_mode

        if safe_mode:
            self._log_safe_mode_start(safe_mode)

        # network setup
        if self._v6_only and not octoprint.util.net.HAS_V6:
            raise RuntimeError(
                "IPv6 only mode configured but system doesn't support IPv6"
            )
        self._ensure_host()
        self._ensure_port()

        self._setup_monkey_patching()
        self._setup_mimetypes()
        self._setup_flask_app(app)
        self._setup_i18n(app)

        # start the intermediary server
        self._start_intermediary_server()

        ### IMPORTANT!
        ###
        ### Best do not start any subprocesses until the intermediary server shuts down again or they MIGHT inherit the
        ### open port and prevent us from firing up Tornado later.
        ###
        ### The intermediary server's socket should have the CLOSE_EXEC flag (or its equivalent) set where possible, but
        ### we can only do that if fcntl is available or we are on Windows, so better safe than sorry.
        ###
        ### See also issues #2035 and #2090

        systemCommandManager = system_command_manager()
        printerProfileManager = PrinterProfileManager()
        eventManager = self._event_manager

        self._setup_analysis_queue()
        self._setup_slicing_manager()
        self._setup_file_manager()

        pluginLifecycleManager = LifecycleManager(self._plugin_manager)

        preemptiveCache = PreemptiveCache(
            os.path.join(
                self._settings.getBaseFolder("data"), "preemptive_cache_config.yaml"
            )
        )

        self._setup_json_encoding()
        self._setup_connectivity_checker()

        environmentDetector = self._environment_detector

        components = {
            "plugin_manager": self._plugin_manager,
            "printer_profile_manager": printerProfileManager,
            "event_bus": eventManager,
            "analysis_queue": analysisQueue,
            "slicing_manager": slicingManager,
            "file_manager": fileManager,
            "plugin_lifecycle_manager": pluginLifecycleManager,
            "preemptive_cache": preemptiveCache,
            "json_encoder": jsonEncoder,
            "json_decoder": jsonDecoder,
            "connectivity_checker": connectivityChecker,
            "environment_detector": self._environment_detector,
            "system_commands": systemCommandManager,
        }

        # ~~ setup access control

        # get additional permissions from plugins
        self._setup_plugin_permissions()

        self._setup_group_manager(components)
        components.update({"group_manager": groupManager})

        self._setup_user_manager(components)
        components.update({"user_manager": userManager})

        self._setup_printer(components)
        components.update({"printer": printer})

        self._setup_plugin_manager(components)

        # log environment data now
        self._environment_detector.log_detected_environment()

        self._setup_jinja2()
        self._setup_assets()
        self._setup_timelapse()
        self._setup_command_triggers()
        self._setup_login_manager()
        self._setup_blueprints()
        self._check_simple_api_plugins()

        ## Tornado initialization starts here

        enable_cors = settings().getBoolean(["api", "allowCrossOrigin"])
        util.tornado.RequestlessExceptionLoggingMixin.LOG_REQUEST = debug
        util.tornado.CorsSupportMixin.ENABLE_CORS = enable_cors

        self._start_event_loop()  # manually start the event loop

        self._tornado_app = self._setup_tornado_app(enable_cors=enable_cors)

        # this can take a bit, so we do it while the intermediary server is still running
        max_body_sizes = self._get_max_body_sizes()

        self._stop_intermediary_server()

        # initialize and bind the actual server
        self._server = self._initialize_and_bind_server(max_body_sizes=max_body_sizes)

        ### From now on it's ok to launch subprocesses again

        eventManager.fire(events.Events.STARTUP)

        self._start_analysis_backlog()
        self._start_serial_autoconnect()
        self._start_serial_autorefresh()
        self._start_watched_observer()
        self._call_startup_plugins()
        self._trigger_after_startup()

        self._register_shutdown_handlers()

        try:
            # this is the main loop - as long as tornado is running, OctoPrint is running
            from tornado.ioloop import IOLoop

            IOLoop.current().start()

            self._logger.debug("Tornado's IOLoop stopped")
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception:
            self._logger.fatal(
                "Now that is embarrassing... Something went really really wrong here. Please report this including the stacktrace below in OctoPrint's bugtracker. Thanks!"
            )
            self._logger.exception("Stacktrace follows:")

    def _log_safe_mode_start(self, self_mode):
        self_mode_file = os.path.join(
            self._settings.getBaseFolder("data"), "last_safe_mode"
        )
        try:
            with open(self_mode_file, "w+", encoding="utf-8") as f:
                f.write(self_mode)
        except Exception as ex:
            self._logger.warn(f"Could not write safe mode file {self_mode_file}: {ex}")

    def _create_socket_connection(self, session):
        global printer, fileManager, analysisQueue, userManager, eventManager, connectivityChecker
        return util.sockjs.PrinterStateConnection(
            printer,
            fileManager,
            analysisQueue,
            userManager,
            groupManager,
            eventManager,
            pluginManager,
            connectivityChecker,
            session,
        )

    def _check_for_root(self):
        if "geteuid" in dir(os) and os.geteuid() == 0:
            exit("You should not run OctoPrint as root!")

    def _get_locale(self):
        global LANGUAGES

        if "l10n" in request.values:
            return Locale.negotiate([request.values["l10n"]], LANGUAGES)

        if "X-Locale" in request.headers:
            return Locale.negotiate([request.headers["X-Locale"]], LANGUAGES)

        if hasattr(g, "identity") and g.identity:
            userid = g.identity.id
            try:
                user_language = userManager.get_user_setting(
                    userid, ("interface", "language")
                )
                if user_language is not None and not user_language == "_default":
                    return Locale.negotiate([user_language], LANGUAGES)
            except octoprint.access.users.UnknownUser:
                pass

        default_language = self._settings.get(["appearance", "defaultLanguage"])
        if (
            default_language is not None
            and not default_language == "_default"
            and default_language in LANGUAGES
        ):
            return Locale.negotiate([default_language], LANGUAGES)

        return Locale.parse(request.accept_languages.best_match(LANGUAGES, default="en"))

    def _setup_heartbeat_logging(self):
        logger = logging.getLogger(__name__ + ".heartbeat")

        def log_heartbeat():
            logger.info("Server heartbeat <3")

        interval = settings().getFloat(["server", "heartbeat"])
        logger.info(f"Starting server heartbeat, {interval}s interval")

        timer = octoprint.util.RepeatedTimer(interval, log_heartbeat)
        timer.start()

    def _ensure_host(self):
        if self._host is None:
            host = self._settings.get(["server", "host"])
            if host is None:
                if octoprint.util.net.HAS_V6:
                    host = "::"
                else:
                    host = "0.0.0.0"

            self._host = host

        if ":" in self._host and not octoprint.util.net.HAS_V6:
            raise RuntimeError(
                "IPv6 host address {!r} configured but system doesn't support IPv6".format(
                    self._host
                )
            )

    def _ensure_port(self):
        if self._port is None:
            self._port = self._settings.getInt(["server", "port"])
            if self._port is None:
                self._port = 5000

    def _setup_monkey_patching(self):
        # monkey patch/fix some stuff
        util.tornado.fix_json_encode()
        util.tornado.fix_websocket_check_origin()
        util.tornado.enable_per_message_deflate_extension()
        util.tornado.fix_tornado_xheader_handling()

    def _setup_mimetypes(self):
        # Safety measures for Windows... apparently the mimetypes module takes its translation from the windows
        # registry, and if for some weird reason that gets borked the reported MIME types can be all over the place.
        # Since at least in Chrome that can cause hilarious issues with JS files (refusal to run them and thus a
        # borked UI) we make sure that .js always maps to the correct application/javascript, and also throw in a
        # .css -> text/css for good measure.
        #
        # See #3367
        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("text/css", ".css")

    def _setup_flask_app(self, app):
        global limiter

        from octoprint.server.util.flask import (
            OctoPrintFlaskRequest,
            OctoPrintFlaskResponse,
            OctoPrintJsonProvider,
            OctoPrintSessionInterface,
            PrefixAwareJinjaEnvironment,
            ReverseProxiedEnvironment,
        )

        # we must set this here because setting app.debug will access app.jinja_env
        app.jinja_options = {"autoescape": True}
        app.jinja_environment = PrefixAwareJinjaEnvironment

        app.config["TEMPLATES_AUTO_RELOAD"] = True
        app.config["REMEMBER_COOKIE_DURATION"] = 90 * 24 * 60 * 60  # 90 days
        app.config["REMEMBER_COOKIE_HTTPONLY"] = True
        # REMEMBER_COOKIE_SECURE will be taken care of by our custom cookie handling

        # we must not set this before TEMPLATES_AUTO_RELOAD is set to True or that won't take
        app.debug = self._debug

        # setup octoprint's flask json serialization/deserialization
        app.json = OctoPrintJsonProvider(app)
        app.json.compact = False

        s = settings()

        secret_key = s.get(["server", "secretKey"])
        if not secret_key:
            import secrets

            secret_key = secrets.token_hex()
            s.set(["server", "secretKey"], secret_key)
            s.save()

        app.secret_key = secret_key

        reverse_proxied = ReverseProxiedEnvironment(
            header_prefix=s.get(["server", "reverseProxy", "prefixHeader"]),
            header_scheme=s.get(["server", "reverseProxy", "schemeHeader"]),
            header_host=s.get(["server", "reverseProxy", "hostHeader"]),
            header_server=s.get(["server", "reverseProxy", "serverHeader"]),
            header_port=s.get(["server", "reverseProxy", "portHeader"]),
            prefix=s.get(["server", "reverseProxy", "prefixFallback"]),
            scheme=s.get(["server", "reverseProxy", "schemeFallback"]),
            host=s.get(["server", "reverseProxy", "hostFallback"]),
            server=s.get(["server", "reverseProxy", "serverFallback"]),
            port=s.get(["server", "reverseProxy", "portFallback"]),
        )

        OctoPrintFlaskRequest.environment_wrapper = reverse_proxied
        app.request_class = OctoPrintFlaskRequest
        app.response_class = OctoPrintFlaskResponse
        app.session_interface = OctoPrintSessionInterface()

        @app.before_request
        def before_request():
            g.locale = self._get_locale()

            # used for performance measurement
            g.start_time = time.monotonic()

            if self._debug and "perfprofile" in request.args:
                try:
                    from pyinstrument import Profiler

                    g.perfprofiler = Profiler()
                    g.perfprofiler.start()
                except ImportError:
                    # profiler dependency not installed, ignore
                    pass

        @app.after_request
        def after_request(response):
            # send no-cache headers with all POST responses
            if request.method == "POST":
                response.cache_control.no_cache = True

            response.headers.add("X-Clacks-Overhead", "GNU Terry Pratchett")

            if hasattr(g, "perfprofiler"):
                g.perfprofiler.stop()
                output_html = g.perfprofiler.output_html()
                return make_response(output_html)

            if hasattr(g, "start_time"):
                end_time = time.monotonic()
                duration_ms = int((end_time - g.start_time) * 1000)
                response.headers.add("Server-Timing", f"app;dur={duration_ms}")

            return response

        from octoprint.util.jinja import MarkdownFilter

        MarkdownFilter(app)

        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address

        app.config["RATELIMIT_STRATEGY"] = "fixed-window"

        limiter = Limiter(
            key_func=get_remote_address,
            app=app,
            enabled=s.getBoolean(["devel", "enableRateLimiter"]),
            storage_uri="memory://",
        )

    def _setup_i18n(self, app):
        global babel
        global LOCALES
        global LANGUAGES
        global safe_mode

        dirs = []
        if not safe_mode:
            dirs += [self._settings.getBaseFolder("translations")]
        dirs += [os.path.join(app.root_path, "translations")]

        # translations from plugins
        plugins = octoprint.plugin.plugin_manager().enabled_plugins
        for plugin in plugins.values():
            plugin_translation_dir = os.path.join(plugin.location, "translations")
            if not os.path.isdir(plugin_translation_dir):
                continue
            dirs.append(plugin_translation_dir)

        app.config["BABEL_TRANSLATION_DIRECTORIES"] = ";".join(dirs)

        babel = Babel(app, locale_selector=self._get_locale)

        def get_available_locale_identifiers(locales):
            result = set()

            # add available translations
            for locale in locales:
                result.add(str(locale))

            return result

        with app.app_context():
            LOCALES = babel.list_translations()
        LANGUAGES = get_available_locale_identifiers(LOCALES)

    def _setup_analysis_queue(self):
        global analysisQueue

        analysis_queue_factories = {
            "gcode": octoprint.filemanager.analysis.GcodeAnalysisQueue
        }
        analysis_queue_hooks = self._plugin_manager.get_hooks(
            "octoprint.filemanager.analysis.factory"
        )

        for name, hook in analysis_queue_hooks.items():
            try:
                additional_factories = hook()
                analysis_queue_factories.update(**additional_factories)
            except Exception:
                self._logger.exception(
                    f"Error while processing analysis queues from {name}",
                    extra={"plugin": name},
                )

        analysisQueue = octoprint.filemanager.analysis.AnalysisQueue(
            analysis_queue_factories
        )

    def _setup_slicing_manager(self):
        global slicingManager
        slicingManager = octoprint.slicing.SlicingManager(
            self._settings.getBaseFolder("slicingProfiles"), printerProfileManager
        )

    def _setup_storage_managers(self):
        storage_managers = {}
        storage_managers[octoprint.filemanager.FileDestinations.LOCAL] = (
            octoprint.filemanager.storage.LocalFileStorage(
                self._settings.getBaseFolder("uploads"),
                really_universal=self._settings.getBoolean(
                    ["feature", "enforceReallyUniversalFilenames"]
                ),
            )
        )
        return storage_managers

    def _setup_file_manager(self):
        global fileManager

        storage_managers = self._setup_storage_managers()

        fileManager = octoprint.filemanager.FileManager(
            analysisQueue,
            slicingManager,
            printerProfileManager,
            initial_storage_managers=storage_managers,
        )

    def _setup_json_encoding(self):
        JsonEncoding.add_encoder(users.User, lambda obj: obj.as_dict())
        JsonEncoding.add_encoder(groups.Group, lambda obj: obj.as_dict())
        JsonEncoding.add_encoder(
            permissions.OctoPrintPermission, lambda obj: obj.as_dict()
        )

    def _setup_connectivity_checker(self):
        global connectivityChecker

        # start regular check if we are connected to the internet
        def on_connectivity_change(old_value, new_value):
            eventManager.fire(
                events.Events.CONNECTIVITY_CHANGED,
                payload={"old": old_value, "new": new_value},
            )

        connectivityChecker = self._connectivity_checker

        def on_settings_update(*args, **kwargs):
            # make sure our connectivity checker runs with the latest settings
            connectivityEnabled = self._settings.getBoolean(
                ["server", "onlineCheck", "enabled"]
            )
            connectivityInterval = self._settings.getInt(
                ["server", "onlineCheck", "interval"]
            )
            connectivityHost = self._settings.get(["server", "onlineCheck", "host"])
            connectivityPort = self._settings.getInt(["server", "onlineCheck", "port"])
            connectivityName = self._settings.get(["server", "onlineCheck", "name"])

            if (
                connectivityChecker.enabled != connectivityEnabled
                or connectivityChecker.interval != connectivityInterval
                or connectivityChecker.host != connectivityHost
                or connectivityChecker.port != connectivityPort
                or connectivityChecker.name != connectivityName
            ):
                connectivityChecker.enabled = connectivityEnabled
                connectivityChecker.interval = connectivityInterval
                connectivityChecker.host = connectivityHost
                connectivityChecker.port = connectivityPort
                connectivityChecker.name = connectivityName
                connectivityChecker.check_immediately()

        eventManager.subscribe(events.Events.SETTINGS_UPDATED, on_settings_update)

    def _setup_plugin_permissions(self):
        from octoprint.access.permissions import PluginOctoPrintPermission

        key_whitelist = re.compile(r"[A-Za-z0-9_]*")

        def permission_key(plugin, definition):
            return "PLUGIN_{}_{}".format(plugin.upper(), definition["key"].upper())

        def permission_name(plugin, definition):
            return "{}: {}".format(plugin, definition["name"])

        def permission_role(plugin, role):
            return f"plugin_{plugin}_{role}"

        def process_regular_permission(plugin_info, definition):
            permissions = []
            for key in definition.get("permissions", []):
                permission = octoprint.access.permissions.Permissions.find(key)

                if permission is None:
                    # if there is still no permission found, postpone this - maybe it is a permission from
                    # another plugin that hasn't been loaded yet
                    return False

                permissions.append(permission)

            roles = definition.get("roles", [])
            description = definition.get("description", "")
            dangerous = definition.get("dangerous", False)
            default_groups = definition.get("default_groups", [])

            roles_and_permissions = [
                permission_role(plugin_info.key, role) for role in roles
            ] + permissions

            key = permission_key(plugin_info.key, definition)
            permission = PluginOctoPrintPermission(
                permission_name(plugin_info.name, definition),
                description,
                *roles_and_permissions,
                plugin=plugin_info.key,
                dangerous=dangerous,
                default_groups=default_groups,
            )
            setattr(
                octoprint.access.permissions.Permissions,
                key,
                permission,
            )

            self._logger.info(
                "Added new permission from plugin {}: {} (needs: {!r})".format(
                    plugin_info.key, key, ", ".join(map(repr, permission.needs))
                )
            )
            return True

        postponed = []

        hooks = self._plugin_manager.get_hooks("octoprint.access.permissions")
        for name, factory in hooks.items():
            try:
                if isinstance(factory, (tuple, list)):
                    additional_permissions = list(factory)
                elif callable(factory):
                    additional_permissions = factory()
                else:
                    raise ValueError("factory must be either a callable, tuple or list")

                if not isinstance(additional_permissions, (tuple, list)):
                    raise ValueError(
                        "factory result must be either a tuple or a list of permission definition dicts"
                    )

                plugin_info = self._plugin_manager.get_plugin_info(name)
                for p in additional_permissions:
                    if not isinstance(p, dict):
                        continue

                    if "key" not in p or "name" not in p:
                        continue

                    if not key_whitelist.match(p["key"]):
                        self._logger.warning(
                            "Got permission with invalid key from plugin {}: {}".format(
                                name, p["key"]
                            )
                        )
                        continue

                    if not process_regular_permission(plugin_info, p):
                        postponed.append((plugin_info, p))
            except Exception:
                self._logger.exception(
                    f"Error while creating permission instance/s from {name}"
                )

        # final resolution passes
        pass_number = 1
        still_postponed = []
        while len(postponed):
            start_length = len(postponed)
            self._logger.debug(
                "Plugin permission resolution pass #{}, "
                "{} unresolved permissions...".format(pass_number, start_length)
            )

            for plugin_info, definition in postponed:
                if not process_regular_permission(plugin_info, definition):
                    still_postponed.append((plugin_info, definition))

            self._logger.debug(
                "... pass #{} done, {} permissions left to resolve".format(
                    pass_number, len(still_postponed)
                )
            )

            if len(still_postponed) == start_length:
                # no change, looks like some stuff is unresolvable - let's bail
                for plugin_info, definition in still_postponed:
                    self._logger.warning(
                        "Unable to resolve permission from {}: {!r}".format(
                            plugin_info.key, definition
                        )
                    )
                break

            postponed = still_postponed
            still_postponed = []
            pass_number += 1

    def _setup_group_manager(self, components):
        global groupManager

        # create group manager instance
        group_manager_factories = self._plugin_manager.get_hooks(
            "octoprint.access.groups.factory"
        )
        for name, factory in group_manager_factories.items():
            try:
                groupManager = factory(components, self._settings)
                if groupManager is not None:
                    self._logger.debug(
                        f"Created group manager instance from factory {name}"
                    )
                    break
            except Exception:
                self._logger.exception(
                    "Error while creating group manager instance from factory {}".format(
                        name
                    )
                )
        else:
            group_manager_name = self._settings.get(["accessControl", "groupManager"])
            try:
                clazz = octoprint.util.get_class(group_manager_name)
                groupManager = clazz()
            except AttributeError:
                self._logger.exception(
                    "Could not instantiate group manager {}, "
                    "falling back to FilebasedGroupManager!".format(group_manager_name)
                )
                groupManager = octoprint.access.groups.FilebasedGroupManager()

    def _setup_user_manager(self, components):
        global userManager

        # create user manager instance
        user_manager_factories = self._plugin_manager.get_hooks(
            "octoprint.users.factory"
        )  # legacy, set first so that new wins
        user_manager_factories.update(
            self._plugin_manager.get_hooks("octoprint.access.users.factory")
        )
        for name, factory in user_manager_factories.items():
            try:
                userManager = factory(components, self._settings)
                if userManager is not None:
                    self._logger.debug(
                        f"Created user manager instance from factory {name}"
                    )
                    break
            except Exception:
                self._logger.exception(
                    "Error while creating user manager instance from factory {}".format(
                        name
                    ),
                    extra={"plugin": name},
                )
        else:
            user_manager_name = self._settings.get(["accessControl", "userManager"])
            try:
                clazz = octoprint.util.get_class(user_manager_name)
                userManager = clazz(groupManager)
            except octoprint.access.users.CorruptUserStorage:
                raise
            except Exception:
                self._logger.exception(
                    "Could not instantiate user manager {}, "
                    "falling back to FilebasedUserManager!".format(user_manager_name)
                )
                userManager = octoprint.access.users.FilebasedUserManager(groupManager)

    def _setup_printer(self, components):
        global analysisQueue
        global fileManager
        global printerProfileManager
        global printer

        # create printer instance
        printer_factories = self._plugin_manager.get_hooks("octoprint.printer.factory")
        for name, factory in printer_factories.items():
            try:
                printer = factory(components)
                if printer is not None:
                    self._logger.debug(f"Created printer instance from factory {name}")
                    break
            except Exception:
                self._logger.exception(
                    f"Error while creating printer instance from factory {name}",
                    extra={"plugin": name},
                )
        else:
            printer = Printer(fileManager, analysisQueue, printerProfileManager)

    def _setup_plugin_manager(self, components):
        from octoprint import (
            init_custom_events,
            init_settings_plugin_config_migration_and_cleanup,
            init_webcam_compat_overlay,
        )
        from octoprint import octoprint_plugin_inject_factory as opif
        from octoprint import settings_plugin_inject_factory as spif

        init_custom_events(self._plugin_manager)

        octoprint_plugin_inject_factory = opif(self._settings, components)
        settings_plugin_inject_factory = spif(self._settings)

        self._plugin_manager.implementation_inject_factories = [
            octoprint_plugin_inject_factory,
            settings_plugin_inject_factory,
        ]
        self._plugin_manager.initialize_implementations()

        init_settings_plugin_config_migration_and_cleanup(self._plugin_manager)
        init_webcam_compat_overlay(self._settings, self._plugin_manager)

        self._plugin_manager.log_all_plugins()

        # initialize file manager and register it for changes in the registered plugins
        fileManager.initialize()
        pluginLifecycleManager.add_callback(
            ["enabled", "disabled"], lambda name, plugin: fileManager.reload_plugins()
        )

        # initialize slicing manager and register it for changes in the registered plugins
        slicingManager.initialize()
        pluginLifecycleManager.add_callback(
            ["enabled", "disabled"],
            lambda name, plugin: slicingManager.reload_slicers(),
        )

    def _setup_jinja2(self):
        import re

        app.jinja_env.add_extension("jinja2.ext.do")
        app.jinja_env.add_extension("octoprint.util.jinja.trycatch")
        app.jinja_env.add_extension("octoprint.util.jinja.autoesc")

        def regex_replace(s, find, replace):
            return re.sub(find, replace, s)

        html_header_regex = re.compile(
            r"<h(?P<number>[1-6])>(?P<content>.*?)</h(?P=number)>"
        )

        def offset_html_headers(s, offset):
            def repl(match):
                number = int(match.group("number"))
                number += offset
                if number > 6:
                    number = 6
                elif number < 1:
                    number = 1
                return "<h{number}>{content}</h{number}>".format(
                    number=number, content=match.group("content")
                )

            return html_header_regex.sub(repl, s)

        markdown_header_regex = re.compile(
            r"^(?P<hashes>#+)\s+(?P<content>.*)$", flags=re.MULTILINE
        )

        def offset_markdown_headers(s, offset):
            def repl(match):
                number = len(match.group("hashes"))
                number += offset
                if number > 6:
                    number = 6
                elif number < 1:
                    number = 1
                return "{hashes} {content}".format(
                    hashes="#" * number, content=match.group("content")
                )

            return markdown_header_regex.sub(repl, s)

        html_link_regex = re.compile(r"<(?P<tag>a.*?)>(?P<content>.*?)</a>")

        def externalize_links(text):
            def repl(match):
                tag = match.group("tag")
                if "href" not in tag:
                    return match.group(0)

                if "target=" not in tag and "rel=" not in tag:
                    tag += ' target="_blank" rel="noreferrer noopener"'

                content = match.group("content")
                return f"<{tag}>{content}</a>"

            return html_link_regex.sub(repl, text)

        single_quote_regex = re.compile("(?<!\\\\)'")

        def escape_single_quote(text):
            return single_quote_regex.sub("\\'", text)

        double_quote_regex = re.compile('(?<!\\\\)"')

        def escape_double_quote(text):
            return double_quote_regex.sub('\\"', text)

        app.jinja_env.filters["regex_replace"] = regex_replace
        app.jinja_env.filters["offset_html_headers"] = offset_html_headers
        app.jinja_env.filters["offset_markdown_headers"] = offset_markdown_headers
        app.jinja_env.filters["externalize_links"] = externalize_links
        app.jinja_env.filters["escape_single_quote"] = app.jinja_env.filters["esq"] = (
            escape_single_quote
        )
        app.jinja_env.filters["escape_double_quote"] = app.jinja_env.filters["edq"] = (
            escape_double_quote
        )

        # configure additional template folders for jinja2
        import jinja2

        import octoprint.util.jinja

        app.jinja_env.prefix_loader = jinja2.PrefixLoader({})

        loaders = [app.jinja_loader, app.jinja_env.prefix_loader]
        if octoprint.util.is_running_from_source():
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
            allowed = ["AUTHORS.md", "SUPPORTERS.md", "THIRDPARTYLICENSES.md"]
            files = {"_data/" + name: os.path.join(root, name) for name in allowed}
            loaders.append(octoprint.util.jinja.SelectedFilesLoader(files))

        # TODO: Remove this in 2.0.0
        warning_message = "Loading plugin template '{template}' from '{filename}' without plugin prefix, this is deprecated and will soon no longer be supported."
        loaders.append(
            octoprint.util.jinja.WarningLoader(
                octoprint.util.jinja.PrefixChoiceLoader(app.jinja_env.prefix_loader),
                warning_message,
            )
        )

        app.jinja_loader = jinja2.ChoiceLoader(loaders)

        self._register_template_plugins()

        # make sure plugin lifecycle events relevant for jinja2 are taken care of
        def template_enabled(name, plugin):
            if plugin.implementation is None or not isinstance(
                plugin.implementation, octoprint.plugin.TemplatePlugin
            ):
                return
            self._register_additional_template_plugin(plugin.implementation)

        def template_disabled(name, plugin):
            if plugin.implementation is None or not isinstance(
                plugin.implementation, octoprint.plugin.TemplatePlugin
            ):
                return
            self._unregister_additional_template_plugin(plugin.implementation)

        pluginLifecycleManager.add_callback("enabled", template_enabled)
        pluginLifecycleManager.add_callback("disabled", template_disabled)

    def _register_template_plugins(self):
        template_plugins = self._plugin_manager.get_implementations(
            octoprint.plugin.TemplatePlugin
        )
        for plugin in template_plugins:
            try:
                self._register_additional_template_plugin(plugin)
            except Exception:
                self._logger.exception(
                    "Error while trying to register templates of plugin {}, ignoring it".format(
                        plugin._identifier
                    )
                )

    def _register_additional_template_plugin(self, plugin):
        import octoprint.util.jinja
        from octoprint.plugin import PluginFlags

        folder = plugin.get_template_folder()
        if (
            folder is not None
            and plugin.template_folder_key not in app.jinja_env.prefix_loader.mapping
        ):
            loader = octoprint.util.jinja.FilteredFileSystemLoader(
                [folder],
                path_filter=lambda x: not octoprint.util.is_hidden_path(x),
            )
            if PluginFlags.AUTOESCAPE_ON not in plugin._plugin_info.flags and (
                PluginFlags.AUTOESCAPE_OFF in plugin._plugin_info.flags
                or (
                    not plugin._plugin_info.bundled
                    and not plugin.is_template_autoescaped()
                )
            ):
                loader = octoprint.util.jinja.PostProcessWrapperLoader(
                    loader,
                    lambda source: "{% autoesc false %}" + source + "{% autoesc true %}",
                )

            app.jinja_env.prefix_loader.mapping[plugin.template_folder_key] = loader

    def _unregister_additional_template_plugin(self, plugin):
        folder = plugin.get_template_folder()
        if (
            folder is not None
            and plugin.template_folder_key in app.jinja_env.prefix_loader.mapping
        ):
            del app.jinja_env.prefix_loader.mapping[plugin.template_folder_key]

    def _setup_assets(self):
        global app
        global assets

        from octoprint.server.util.webassets import MemoryManifest  # noqa: F401

        util.flask.fix_webassets_convert_item_to_flask_url()
        util.flask.fix_webassets_filtertool()

        base_folder = self._settings.getBaseFolder("generated")

        # clean the folder
        if self._settings.getBoolean(["devel", "webassets", "clean_on_startup"]):
            import errno
            import shutil

            for entry, recreate in (
                ("webassets", True),
                # no longer used, but clean up just in case
                (".webassets-cache", False),
                (".webassets-manifest.json", False),
            ):
                path = os.path.join(base_folder, entry)

                # delete path if it exists
                if os.path.exists(path):
                    try:
                        self._logger.debug(f"Deleting {path}...")
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        else:
                            os.remove(path)
                    except Exception:
                        self._logger.exception(
                            f"Error while trying to delete {path}, leaving it alone"
                        )
                        continue

                # re-create path if necessary
                if recreate:
                    self._logger.debug(f"Creating {path}...")
                    error_text = (
                        f"Error while trying to re-create {path}, that might cause "
                        f"errors with the webassets cache"
                    )
                    try:
                        os.makedirs(path)
                    except OSError as e:
                        if e.errno == errno.EACCES:
                            # that might be caused by the user still having the folder open somewhere, let's try again after
                            # waiting a bit
                            import time

                            for n in range(3):
                                time.sleep(0.5)
                                self._logger.debug(
                                    "Creating {path}: Retry #{retry} after {time}s".format(
                                        path=path, retry=n + 1, time=(n + 1) * 0.5
                                    )
                                )
                                try:
                                    os.makedirs(path)
                                    break
                                except Exception:
                                    if self._logger.isEnabledFor(logging.DEBUG):
                                        self._logger.exception(
                                            f"Ignored error while creating "
                                            f"directory {path}"
                                        )
                                    pass
                            else:
                                # this will only get executed if we never did
                                # successfully execute makedirs above
                                self._logger.exception(error_text)
                                continue
                        else:
                            # not an access error, so something we don't understand
                            # went wrong -> log an error and stop
                            self._logger.exception(error_text)
                            continue
                    except Exception:
                        # not an OSError, so something we don't understand
                        # went wrong -> log an error and stop
                        self._logger.exception(error_text)
                        continue

                self._logger.info(f"Reset webasset folder {path}...")

        AdjustedEnvironment = type(Environment)(
            Environment.__name__,
            (Environment,),
            {"resolver_class": util.flask.PluginAssetResolver},
        )

        class CustomDirectoryEnvironment(AdjustedEnvironment):
            @property
            def directory(self):
                return base_folder

        assets = CustomDirectoryEnvironment(app)
        assets.debug = not self._settings.getBoolean(["devel", "webassets", "bundle"])

        # we should rarely if ever regenerate the webassets in production and can wait a
        # few seconds for regeneration in development, if it means we can get rid of
        # a whole monkey patch and in internal use of pickle with non-tamperproof files
        assets.cache = False
        assets.manifest = "memory"

        UpdaterType = type(util.flask.SettingsCheckUpdater)(
            util.flask.SettingsCheckUpdater.__name__,
            (util.flask.SettingsCheckUpdater,),
            {"updater": assets.updater},
        )
        assets.updater = UpdaterType

        preferred_stylesheet = self._settings.get(["devel", "stylesheet"])

        dynamic_core_assets = util.flask.collect_core_assets()
        dynamic_plugin_assets = util.flask.collect_plugin_assets(
            preferred_stylesheet=preferred_stylesheet
        )

        js_libs = [
            "js/lib/babel-polyfill.min.js",
            "js/lib/jquery/jquery.min.js",
            "js/lib/modernizr.custom.js",
            "js/lib/lodash.min.js",
            "js/lib/sprintf.min.js",
            "js/lib/knockout.js",
            "js/lib/knockout.mapping-latest.js",
            "js/lib/babel.js",
            "js/lib/bootstrap/bootstrap.js",
            "js/lib/bootstrap/bootstrap-modalmanager.js",
            "js/lib/bootstrap/bootstrap-modal.js",
            "js/lib/bootstrap/bootstrap-slider.js",
            "js/lib/bootstrap/bootstrap-tabdrop.js",
            "js/lib/jquery/jquery-ui.js",
            "js/lib/jquery/jquery.flot.js",
            "js/lib/jquery/jquery.flot.time.js",
            "js/lib/jquery/jquery.flot.crosshair.js",
            "js/lib/jquery/jquery.flot.dashes.js",
            "js/lib/jquery/jquery.flot.resize.js",
            "js/lib/jquery/jquery.iframe-transport.js",
            "js/lib/jquery/jquery.fileupload.js",
            "js/lib/jquery/jquery.slimscroll.min.js",
            "js/lib/jquery/jquery.qrcode.min.js",
            "js/lib/jquery/jquery.bootstrap.wizard.js",
            "js/lib/pnotify/pnotify.core.min.js",
            "js/lib/pnotify/pnotify.buttons.min.js",
            "js/lib/pnotify/pnotify.callbacks.min.js",
            "js/lib/pnotify/pnotify.confirm.min.js",
            "js/lib/pnotify/pnotify.desktop.min.js",
            "js/lib/pnotify/pnotify.history.min.js",
            "js/lib/pnotify/pnotify.mobile.min.js",
            "js/lib/pnotify/pnotify.nonblock.min.js",
            "js/lib/pnotify/pnotify.reference.min.js",
            "js/lib/pnotify/pnotify.tooltip.min.js",
            "js/lib/pnotify/pnotify.maxheight.js",
            "js/lib/moment-with-locales.min.js",
            "js/lib/pusher.color.min.js",
            "js/lib/detectmobilebrowser.js",
            "js/lib/ua-parser.min.js",
            "js/lib/md5.min.js",
            "js/lib/bootstrap-slider-knockout-binding.js",
            "js/lib/loglevel.min.js",
            "js/lib/sockjs.min.js",
            "js/lib/hls.js",
            "js/lib/less.js",
        ]

        css_libs = [
            "css/bootstrap.min.css",
            "css/bootstrap-modal.css",
            "css/bootstrap-slider.css",
            "css/bootstrap-tabdrop.css",
            "vendor/font-awesome-3.2.1/css/font-awesome.min.css",
            "vendor/font-awesome-6.5.1/css/all.min.css",
            "vendor/font-awesome-6.5.1/css/v4-shims.min.css",
            "vendor/fa5-power-transforms.min.css",
            "css/jquery.fileupload-ui.css",
            "css/pnotify.core.min.css",
            "css/pnotify.buttons.min.css",
            "css/pnotify.history.min.css",
        ]

        # a couple of custom filters
        from webassets.filter import register_filter

        from octoprint.server.util.webassets import (
            GzipFile,
            JsDelimiterBundler,
            JsPluginBundle,
            LessImportRewrite,
            RJSMinExtended,
            SourceMapRemove,
            SourceMapRewrite,
        )

        register_filter(LessImportRewrite)
        register_filter(SourceMapRewrite)
        register_filter(SourceMapRemove)
        register_filter(JsDelimiterBundler)
        register_filter(GzipFile)
        register_filter(RJSMinExtended)

        def all_assets_for_plugins(collection):
            """Gets all plugin assets for a dict of plugin->assets"""
            result = []
            for assets in collection.values():
                result += assets
            return result

        # -- JS --------------------------------------------------------------------------------------------------------

        filters = ["sourcemap_remove"]
        if self._settings.getBoolean(["devel", "webassets", "minify"]):
            filters += ["rjsmin_extended"]
        filters += ["js_delimiter_bundler", "gzip"]

        js_filters = filters
        if self._settings.getBoolean(["devel", "webassets", "minify_plugins"]):
            js_plugin_filters = js_filters
        else:
            js_plugin_filters = [x for x in js_filters if x not in ("rjsmin_extended",)]

        def js_bundles_for_plugins(collection, filters=None):
            """Produces JsPluginBundle instances that output IIFE wrapped assets"""
            result = OrderedDict()
            for plugin, assets in collection.items():
                if len(assets):
                    result[plugin] = JsPluginBundle(plugin, *assets, filters=filters)
            return result

        js_core = (
            dynamic_core_assets["js"]
            + all_assets_for_plugins(dynamic_plugin_assets["bundled"]["js"])
            + ["js/app/dataupdater.js", "js/app/helpers.js", "js/app/main.js"]
        )
        js_plugins = js_bundles_for_plugins(
            dynamic_plugin_assets["external"]["js"], filters="js_delimiter_bundler"
        )

        clientjs_core = dynamic_core_assets["clientjs"] + all_assets_for_plugins(
            dynamic_plugin_assets["bundled"]["clientjs"]
        )
        clientjs_plugins = js_bundles_for_plugins(
            dynamic_plugin_assets["external"]["clientjs"],
            filters="js_delimiter_bundler",
        )

        js_libs_bundle = Bundle(
            *js_libs, output="webassets/packed_libs.js", filters=",".join(js_filters)
        )

        js_core_bundle = Bundle(
            *js_core, output="webassets/packed_core.js", filters=",".join(js_filters)
        )

        if len(js_plugins) == 0:
            js_plugins_bundle = Bundle(*[])
        else:
            js_plugins_bundle = Bundle(
                *js_plugins.values(),
                output="webassets/packed_plugins.js",
                filters=",".join(js_plugin_filters),
            )

        js_app_bundle = Bundle(
            js_plugins_bundle,
            js_core_bundle,
            output="webassets/packed_app.js",
            filters=",".join(js_plugin_filters),
        )

        js_client_core_bundle = Bundle(
            *clientjs_core,
            output="webassets/packed_client_core.js",
            filters=",".join(js_filters),
        )

        if len(clientjs_plugins) == 0:
            js_client_plugins_bundle = Bundle(*[])
        else:
            js_client_plugins_bundle = Bundle(
                *clientjs_plugins.values(),
                output="webassets/packed_client_plugins.js",
                filters=",".join(js_plugin_filters),
            )

        js_client_bundle = Bundle(
            js_client_core_bundle,
            js_client_plugins_bundle,
            output="webassets/packed_client.js",
            filters=",".join(js_plugin_filters),
        )

        # -- CSS -------------------------------------------------------------------------------------------------------

        css_filters = ["cssrewrite", "gzip"]

        css_core = list(dynamic_core_assets["css"]) + all_assets_for_plugins(
            dynamic_plugin_assets["bundled"]["css"]
        )
        css_plugins = list(
            all_assets_for_plugins(dynamic_plugin_assets["external"]["css"])
        )

        css_libs_bundle = Bundle(
            *css_libs, output="webassets/packed_libs.css", filters=",".join(css_filters)
        )

        if len(css_core) == 0:
            css_core_bundle = Bundle(*[])
        else:
            css_core_bundle = Bundle(
                *css_core,
                output="webassets/packed_core.css",
                filters=",".join(css_filters),
            )

        if len(css_plugins) == 0:
            css_plugins_bundle = Bundle(*[])
        else:
            css_plugins_bundle = Bundle(
                *css_plugins,
                output="webassets/packed_plugins.css",
                filters=",".join(css_filters),
            )

        css_app_bundle = Bundle(
            css_core,
            css_plugins,
            output="webassets/packed_app.css",
            filters=",".join(css_filters),
        )

        # -- LESS ------------------------------------------------------------------------------------------------------

        less_filters = ["cssrewrite", "less_importrewrite", "gzip"]

        less_core = list(dynamic_core_assets["less"]) + all_assets_for_plugins(
            dynamic_plugin_assets["bundled"]["less"]
        )
        less_plugins = all_assets_for_plugins(dynamic_plugin_assets["external"]["less"])

        if len(less_core) == 0:
            less_core_bundle = Bundle(*[])
        else:
            less_core_bundle = Bundle(
                *less_core,
                output="webassets/packed_core.less",
                filters=",".join(less_filters),
            )

        if len(less_plugins) == 0:
            less_plugins_bundle = Bundle(*[])
        else:
            less_plugins_bundle = Bundle(
                *less_plugins,
                output="webassets/packed_plugins.less",
                filters=",".join(less_filters),
            )

        less_app_bundle = Bundle(
            less_core,
            less_plugins,
            output="webassets/packed_app.less",
            filters=",".join(less_filters),
        )

        # -- asset registration ----------------------------------------------------------------------------------------

        assets.register("js_libs", js_libs_bundle)
        assets.register("js_client_core", js_client_core_bundle)
        for plugin, bundle in clientjs_plugins.items():
            # register our collected clientjs plugin bundles so that they are bound to the environment
            assets.register(f"js_client_plugin_{plugin}", bundle)
        assets.register("js_client_plugins", js_client_plugins_bundle)
        assets.register("js_client", js_client_bundle)
        assets.register("js_core", js_core_bundle)
        for plugin, bundle in js_plugins.items():
            # register our collected plugin bundles so that they are bound to the environment
            assets.register(f"js_plugin_{plugin}", bundle)
        assets.register("js_plugins", js_plugins_bundle)
        assets.register("js_app", js_app_bundle)
        assets.register("css_libs", css_libs_bundle)
        assets.register("css_core", css_core_bundle)
        assets.register("css_plugins", css_plugins_bundle)
        assets.register("css_app", css_app_bundle)
        assets.register("less_core", less_core_bundle)
        assets.register("less_plugins", less_plugins_bundle)
        assets.register("less_app", less_app_bundle)

    def _prepare_asset_plugins(self):
        def register_asset_blueprint(plugin, blueprint, url_prefix):
            try:
                app.register_blueprint(
                    blueprint, url_prefix=url_prefix, name_prefix="plugin"
                )
                self._logger.debug(
                    f"Registered assets of plugin {plugin} under URL prefix {url_prefix}"
                )
            except Exception:
                self._logger.exception(
                    f"Error while registering blueprint of plugin {plugin}, ignoring it",
                    extra={"plugin": plugin},
                )

        blueprints = []
        registrators = []

        asset_plugins = octoprint.plugin.plugin_manager().get_implementations(
            octoprint.plugin.AssetPlugin
        )
        for plugin in asset_plugins:
            if isinstance(plugin, octoprint.plugin.BlueprintPlugin):
                continue
            blueprint, prefix = self._prepare_asset_plugin(plugin)

            blueprints.append(blueprint)
            registrators.append(
                functools.partial(
                    register_asset_blueprint, plugin._identifier, blueprint, prefix
                )
            )

        return blueprints, registrators

    def _prepare_asset_plugin(self, plugin):
        name = plugin._identifier

        url_prefix = f"/plugin/{name}"
        blueprint = Blueprint(name, name, static_folder=plugin.get_asset_folder())

        blueprint.before_request(corsRequestHandler)
        blueprint.after_request(corsResponseHandler)

        return blueprint, url_prefix

    def _setup_timelapse(self):
        # configure timelapse
        octoprint.timelapse.valid_timelapse("test")
        octoprint.timelapse.configure_timelapse()
        octoprint.timelapse.setup_rendering_queue()

    def _setup_command_triggers(self):
        global printer

        events.CommandTrigger(printer)
        if self._debug:
            events.DebugEventListener()

    def _setup_login_manager(self):
        global loginManager

        loginManager = LoginManager()

        # "strong" is incompatible to remember me, see maxcountryman/flask-login#156. It also causes issues with
        # clients toggling between IPv4 and IPv6 client addresses due to names being resolved one way or the other as
        # at least observed on a Win10 client targeting "localhost", resolved as both "127.0.0.1" and "::1"
        loginManager.session_protection = "basic"

        loginManager.user_loader(load_user)
        loginManager.unauthorized_handler(unauthorized_user)
        loginManager.anonymous_user = userManager.anonymous_user_factory
        loginManager.request_loader(load_user_from_request)

        loginManager.init_app(app, add_context_processor=False)

        global principals
        principals = Principal(app, anonymous_identity=OctoPrintAnonymousIdentity)

        def current_user_identity_loader():
            # load the identity from the current flask_login user
            if (
                current_user is not None
                and current_user.is_active
                and not current_user.is_anonymous
            ):
                return Identity(current_user.get_id())

        principals.identity_loader(current_user_identity_loader)

    def _setup_blueprints(self):
        # do not remove or the index view won't be found
        import octoprint.server.views  # noqa: F401
        from octoprint.server.api import api
        from octoprint.server.util.flask import make_api_error

        blueprints = [api]
        api_endpoints = ["/api"]
        registrators = [functools.partial(app.register_blueprint, api, url_prefix="/api")]

        # also register any blueprints defined in BlueprintPlugins
        (
            blueprints_from_plugins,
            api_endpoints_from_plugins,
            registrators_from_plugins,
        ) = self._prepare_blueprint_plugins()
        blueprints += blueprints_from_plugins
        api_endpoints += api_endpoints_from_plugins
        registrators += registrators_from_plugins

        # and register a blueprint for serving the static files of asset plugins which are not blueprint plugins themselves
        (blueprints_from_assets, registrators_from_assets) = self._prepare_asset_plugins()
        blueprints += blueprints_from_assets
        registrators += registrators_from_assets

        # make sure all before/after_request hook results are attached as well
        self._add_plugin_request_handlers_to_blueprints(*blueprints)

        # register everything with the system
        for registrator in registrators:
            registrator()

        @app.errorhandler(HTTPException)
        def _handle_api_error(ex):
            if any(request.path.startswith(x) for x in api_endpoints):
                return make_api_error(ex.description, ex.code)
            else:
                return ex

    def _prepare_blueprint_plugins(self):
        def register_plugin_blueprint(plugin, blueprint, url_prefix):
            try:
                app.register_blueprint(
                    blueprint, url_prefix=url_prefix, name_prefix="plugin"
                )
                self._logger.debug(
                    f"Registered API of plugin {plugin} under URL prefix {url_prefix}"
                )
            except Exception:
                self._logger.exception(
                    f"Error while registering blueprint of plugin {plugin}, ignoring it",
                    extra={"plugin": plugin},
                )

        blueprints = []
        api_endpoints = []
        registrators = []

        blueprint_plugins = octoprint.plugin.plugin_manager().get_implementations(
            octoprint.plugin.BlueprintPlugin
        )
        for plugin in blueprint_plugins:
            blueprint, prefix = self._prepare_blueprint_plugin(plugin)

            blueprints.append(blueprint)
            api_endpoints += (prefix + x for x in plugin.get_blueprint_api_prefixes())
            registrators.append(
                functools.partial(
                    register_plugin_blueprint, plugin._identifier, blueprint, prefix
                )
            )

        return blueprints, api_endpoints, registrators

    def _prepare_blueprint_plugin(self, plugin):
        name = plugin._identifier
        blueprint = plugin.get_blueprint()
        if blueprint is None:
            return

        blueprint.before_request(corsRequestHandler)
        blueprint.after_request(corsResponseHandler)

        if plugin.is_blueprint_csrf_protected():
            self._logger.debug(
                f"CSRF Protection for Blueprint of plugin {name} is enabled"
            )
            blueprint.before_request(csrfRequestHandler)
        else:
            self._logger.warning(
                f"CSRF Protection for Blueprint of plugin {name} is DISABLED"
            )

        if plugin.is_blueprint_protected():
            blueprint.before_request(requireLoginRequestHandler)

        url_prefix = f"/plugin/{name}"
        return blueprint, url_prefix

    def _add_plugin_request_handlers_to_blueprints(self, *blueprints):
        before_hooks = octoprint.plugin.plugin_manager().get_hooks(
            "octoprint.server.api.before_request"
        )
        after_hooks = octoprint.plugin.plugin_manager().get_hooks(
            "octoprint.server.api.after_request"
        )

        for name, hook in before_hooks.items():
            plugin = octoprint.plugin.plugin_manager().get_plugin(name)
            for blueprint in blueprints:
                try:
                    result = hook(plugin=plugin)
                    if isinstance(result, (list, tuple)):
                        for h in result:
                            blueprint.before_request(h)
                except Exception:
                    self._logger.exception(
                        "Error processing before_request hooks from plugin {}".format(
                            plugin
                        ),
                        extra={"plugin": name},
                    )

        for name, hook in after_hooks.items():
            plugin = octoprint.plugin.plugin_manager().get_plugin(name)
            for blueprint in blueprints:
                try:
                    result = hook(plugin=plugin)
                    if isinstance(result, (list, tuple)):
                        for h in result:
                            blueprint.after_request(h)
                except Exception:
                    self._logger.exception(
                        "Error processing after_request hooks from plugin {}".format(
                            plugin
                        ),
                        extra={"plugin": name},
                    )

    def _check_simple_api_plugins(self):
        api_plugins = octoprint.plugin.plugin_manager().get_implementations(
            octoprint.plugin.SimpleApiPlugin
        )
        for plugin in api_plugins:
            name = plugin._identifier
            try:
                plugin.is_api_protected()
            except Exception:
                self._logger.exception(
                    f"Error checking is_api_protected of plugin {plugin}",
                    extra={"plugin": name},
                )

    def _start_event_loop(self):
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    def _setup_tornado_app(self, enable_cors=False):
        from tornado.web import Application

        added_headers, removed_headers = self._get_header_transforms()

        handlers = self._get_server_handlers(
            added_headers=added_headers,
            removed_headers=removed_headers,
            enable_cors=enable_cors,
        )

        transforms = [
            util.tornado.GlobalHeaderTransform.for_headers(
                "OctoPrintGlobalHeaderTransform",
                headers=added_headers,
                removed_headers=removed_headers,
            )
        ]

        app = Application(handlers=handlers, transforms=transforms)

        return app

    def _get_server_handlers(
        self, added_headers=None, removed_headers=None, enable_cors=False
    ):
        from concurrent.futures import ThreadPoolExecutor

        def mime_type_guesser(path):
            from octoprint.filemanager import get_mime_type

            return get_mime_type(path)

        def download_name_generator(path):
            metadata = fileManager.get_metadata("local", path)
            if metadata and "display" in metadata:
                return metadata["display"]

        download_handler_kwargs = {"as_attachment": True, "allow_client_caching": False}

        additional_mime_types = {"mime_type_guesser": mime_type_guesser}

        ##~~ Permission validators

        access_validators_from_plugins = []
        for plugin, hook in self._plugin_manager.get_hooks(
            "octoprint.server.http.access_validator"
        ).items():
            try:
                access_validators_from_plugins.append(
                    util.tornado.access_validation_factory(app, hook)
                )
            except Exception:
                self._logger.exception(
                    "Error while adding tornado access validator from plugin {}".format(
                        plugin
                    ),
                    extra={"plugin": plugin},
                )

        timelapse_validators = [
            util.tornado.access_validation_factory(
                app,
                util.flask.permission_validator,
                permissions.Permissions.TIMELAPSE_LIST,
            ),
        ] + access_validators_from_plugins
        download_validators = [
            util.tornado.access_validation_factory(
                app,
                util.flask.permission_validator,
                permissions.Permissions.FILES_DOWNLOAD,
            ),
        ] + access_validators_from_plugins
        log_validators = [
            util.tornado.access_validation_factory(
                app,
                util.flask.permission_validator,
                permissions.Permissions.PLUGIN_LOGGING_MANAGE,
            ),
        ] + access_validators_from_plugins
        camera_validators = [
            util.tornado.access_validation_factory(
                app, util.flask.permission_validator, permissions.Permissions.WEBCAM
            ),
        ] + access_validators_from_plugins
        systeminfo_validators = [
            util.tornado.access_validation_factory(
                app, util.flask.permission_validator, permissions.Permissions.SYSTEM
            )
        ] + access_validators_from_plugins

        timelapse_permission_validator = {
            "access_validation": util.tornado.validation_chain(*timelapse_validators)
        }
        download_permission_validator = {
            "access_validation": util.tornado.validation_chain(*download_validators)
        }
        log_permission_validator = {
            "access_validation": util.tornado.validation_chain(*log_validators)
        }
        camera_permission_validator = {
            "access_validation": util.tornado.validation_chain(*camera_validators)
        }
        systeminfo_permission_validator = {
            "access_validation": util.tornado.validation_chain(*systeminfo_validators)
        }

        no_hidden_files_validator = {
            "path_validation": util.tornado.path_validation_factory(
                lambda path: not octoprint.util.is_hidden_path(path), status_code=404
            )
        }

        only_known_types_validator = {
            "path_validation": util.tornado.path_validation_factory(
                lambda path: octoprint.filemanager.valid_file_type(
                    os.path.basename(path)
                ),
                status_code=404,
            )
        }

        bulkdownloads_path_validator = {
            "path_validation": util.tornado.path_validation_factory(
                lambda path: not octoprint.util.is_hidden_path(path)
                and octoprint.filemanager.valid_file_type(os.path.basename(path))
                and os.path.realpath(os.path.abspath(path)).startswith(
                    settings().getBaseFolder("uploads")
                )
            )
        }

        valid_timelapse = lambda path: not octoprint.util.is_hidden_path(path) and (
            octoprint.timelapse.valid_timelapse(path)
            or octoprint.timelapse.valid_timelapse_thumbnail(path)
        )
        timelapse_path_validator = {
            "path_validation": util.tornado.path_validation_factory(
                valid_timelapse,
                status_code=404,
            )
        }
        timelapses_path_validator = {
            "path_validation": util.tornado.path_validation_factory(
                lambda path: valid_timelapse(path)
                and os.path.realpath(os.path.abspath(path)).startswith(
                    settings().getBaseFolder("timelapse")
                ),
                status_code=400,
            )
        }

        valid_log = lambda path: not octoprint.util.is_hidden_path(
            path
        ) and path.endswith(".log")
        log_path_validator = {
            "path_validation": util.tornado.path_validation_factory(
                valid_log,
                status_code=404,
            )
        }
        logs_path_validator = {
            "path_validation": util.tornado.path_validation_factory(
                lambda path: valid_log(path)
                and os.path.realpath(os.path.abspath(path)).startswith(
                    settings().getBaseFolder("logs")
                ),
                status_code=400,
            )
        }

        def joined_dict(*dicts):
            if not len(dicts):
                return {}

            joined = {}
            for d in dicts:
                joined.update(d)
            return joined

        # SockJS

        self._router = SockJSRouter(
            self._create_socket_connection,
            "/sockjs",
            session_kls=util.sockjs.ThreadSafeSession,
            user_settings={
                "websocket_allow_origin": "*" if enable_cors else "",
                "jsessionid": False,
                "sockjs_url": "../../static/js/lib/sockjs.min.js",
            },
        )

        # Various default routes

        server_routes = self._router.urls + [
            # various downloads
            # .mpg and .mp4 timelapses:
            (
                r"/downloads/timelapse/(.*)",
                util.tornado.LargeResponseHandler,
                joined_dict(
                    {"path": self._settings.getBaseFolder("timelapse")},
                    timelapse_permission_validator,
                    download_handler_kwargs,
                    timelapse_path_validator,
                ),
            ),
            # zipped timelapse bundles
            (
                r"/downloads/timelapses",
                util.tornado.DynamicZipBundleHandler,
                joined_dict(
                    {
                        "as_attachment": True,
                        "attachment_name": "octoprint-timelapses.zip",
                        "path_processor": lambda x: (
                            x,
                            os.path.join(self._settings.getBaseFolder("timelapse"), x),
                        ),
                    },
                    timelapse_permission_validator,
                    timelapses_path_validator,
                ),
            ),
            # uploaded printables
            (
                r"/downloads/files/local/(.*)",
                util.tornado.LargeResponseHandler,
                joined_dict(
                    {
                        "path": self._settings.getBaseFolder("uploads"),
                        "as_attachment": True,
                        "name_generator": download_name_generator,
                    },
                    download_permission_validator,
                    download_handler_kwargs,
                    no_hidden_files_validator,
                    only_known_types_validator,
                    additional_mime_types,
                ),
            ),
            # bulk download of uploaded printables
            (
                r"/downloads/files/local",
                util.tornado.DynamicZipBundleHandler,
                joined_dict(
                    {
                        "as_attachment": True,
                        "attachment_name": "octoprint-files.zip",
                        "path_processor": lambda x: (
                            x,
                            os.path.join(
                                self._settings.getBaseFolder("uploads"), *x.split("/")
                            ),
                        ),
                    },
                    download_permission_validator,
                    bulkdownloads_path_validator,
                ),
            ),
            # log files
            (
                r"/downloads/logs/([^/]*)",
                util.tornado.LargeResponseHandler,
                joined_dict(
                    {
                        "path": self._settings.getBaseFolder("logs"),
                        "mime_type_guesser": lambda *args, **kwargs: "text/plain",
                        "stream_body": True,
                    },
                    download_handler_kwargs,
                    log_permission_validator,
                    log_path_validator,
                ),
            ),
            # zipped log file bundles
            (
                r"/downloads/logs",
                util.tornado.DynamicZipBundleHandler,
                joined_dict(
                    {
                        "as_attachment": True,
                        "attachment_name": "octoprint-logs.zip",
                        "path_processor": lambda x: (
                            x,
                            os.path.join(self._settings.getBaseFolder("logs"), x),
                        ),
                    },
                    log_permission_validator,
                    logs_path_validator,
                ),
            ),
            # system info bundle
            (
                r"/downloads/systeminfo.zip",
                util.tornado.SystemInfoBundleHandler,
                systeminfo_permission_validator,
            ),
            # camera snapshot
            (
                r"/downloads/camera/current",
                util.tornado.WebcamSnapshotHandler,
                joined_dict(
                    {
                        "as_attachment": "snapshot",
                    },
                    camera_permission_validator,
                ),
            ),
            # generated webassets
            (
                r"/static/webassets/(.*)",
                util.tornado.LargeResponseHandler,
                {
                    "path": os.path.join(
                        self._settings.getBaseFolder("generated"), "webassets"
                    ),
                    "is_pre_compressed": True,
                },
            ),
            # online indicators - text file with "online" as content and a transparent gif
            (r"/online.txt", util.tornado.StaticDataHandler, {"data": "online\n"}),
            (
                r"/online.gif",
                util.tornado.StaticDataHandler,
                {
                    "data": bytes(
                        base64.b64decode(
                            "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
                        )
                    ),
                    "content_type": "image/gif",
                },
            ),
            # deprecated endpoints
            (
                r"/api/logs",
                util.tornado.DeprecatedEndpointHandler,
                {"url": "/plugin/logging/logs"},
            ),
            (
                r"/api/logs/(.*)",
                util.tornado.DeprecatedEndpointHandler,
                {"url": "/plugin/logging/logs/{0}"},
            ),
        ]

        # additional routes from plugins
        for name, hook in self._plugin_manager.get_hooks(
            "octoprint.server.http.routes"
        ).items():
            try:
                result = hook(list(server_routes))
            except Exception:
                self._logger.exception(
                    f"There was an error while retrieving additional "
                    f"server routes from plugin hook {name}",
                    extra={"plugin": name},
                )
            else:
                if isinstance(result, (list, tuple)):
                    for entry in result:
                        if not isinstance(entry, tuple) or not len(entry) == 3:
                            continue
                        if not isinstance(entry[0], str):
                            continue
                        if not isinstance(entry[2], dict):
                            continue

                        route, handler, kwargs = entry
                        route = r"/plugin/{name}/{route}".format(
                            name=name,
                            route=route if not route.startswith("/") else route[1:],
                        )

                        self._logger.debug(
                            f"Adding additional route {route} handled by handler {handler} and with additional arguments {kwargs!r}"
                        )
                        server_routes.append((route, handler, kwargs))

        # api handler

        upload_suffixes = {
            "name": self._settings.get(["server", "uploads", "nameSuffix"]),
            "path": self._settings.get(["server", "uploads", "pathSuffix"]),
        }

        server_routes.append(
            (
                r".*",
                util.tornado.UploadStorageFallbackHandler,
                {
                    "fallback": util.tornado.WsgiInputContainer(
                        app.wsgi_app,
                        executor=ThreadPoolExecutor(
                            thread_name_prefix="WsgiRequestHandler"
                        ),
                        headers=added_headers,
                        removed_headers=removed_headers,
                    ),
                    "file_prefix": "octoprint-file-upload-",
                    "file_suffix": ".tmp",
                    "suffixes": upload_suffixes,
                },
            )
        )

        return server_routes

    def _get_header_transforms(self):
        added = {
            "X-Robots-Tag": "noindex, nofollow, noimageindex",
            "X-Content-Type-Options": "nosniff",
        }
        if not settings().getBoolean(["server", "allowFraming"]):
            added["X-Frame-Options"] = "sameorigin"

        removed = ["Server"]

        return added, removed

    def _get_max_body_sizes(self):
        max_body_sizes = [
            (
                "POST",
                r"/api/files/([^/]*)",
                self._settings.getInt(["server", "uploads", "maxSize"]),
            ),
            ("POST", r"/api/languages", 5 * 1024 * 1024),
        ]

        # allow plugins to extend allowed maximum body sizes
        for name, hook in self._plugin_manager.get_hooks(
            "octoprint.server.http.bodysize"
        ).items():
            try:
                result = hook(list(max_body_sizes))
            except Exception:
                self._logger.exception(
                    f"There was an error while retrieving additional "
                    f"upload sizes from plugin hook {name}",
                    extra={"plugin": name},
                )
            else:
                if isinstance(result, (list, tuple)):
                    for entry in result:
                        if not isinstance(entry, tuple) or not len(entry) == 3:
                            continue
                        if (
                            entry[0]
                            not in util.tornado.UploadStorageFallbackHandler.BODY_METHODS
                        ):
                            continue
                        if not isinstance(entry[2], int):
                            continue

                        method, route, size = entry
                        route = r"/plugin/{name}/{route}".format(
                            name=name,
                            route=route if not route.startswith("/") else route[1:],
                        )

                        self._logger.debug(
                            f"Adding maximum body size of {size}B for {method} requests to {route})"
                        )
                        max_body_sizes.append((method, route, size))

        return max_body_sizes

    def _initialize_and_bind_server(self, max_body_sizes=None):
        if max_body_sizes is None:
            max_body_sizes = self._get_max_body_sizes()

        trusted_proxies = octoprint.util.net.usable_trusted_proxies_from_settings(
            settings()
        )

        server_kwargs = {
            "max_body_sizes": max_body_sizes,
            "default_max_body_size": self._settings.getInt(["server", "maxSize"]),
            "xheaders": True,
            "trusted_downstream": trusted_proxies,
        }
        if sys.platform == "win32":
            # set 10min idle timeout under windows to hopefully make #2916 less likely
            server_kwargs.update({"idle_connection_timeout": 600})

        # initialize
        server = util.tornado.CustomHTTPServer(self._tornado_app, **server_kwargs)

        # bind
        address = self._host
        if self._host == "::" and not self._v6_only:
            # special case - tornado only listens on v4 _and_ v6 if we use None as address
            address = None
        server.listen(self._port, address=address)

        return server

    def _start_analysis_backlog(self):
        # analysis backlog
        fileManager.process_backlog()

    def _start_serial_autoconnect(self):
        if not self._settings.getBoolean(["serial", "autoconnect"]):
            return

        self._logger.info(
            "Autoconnect on startup is configured, trying to connect to the printer..."
        )
        try:
            (port, baudrate) = (
                self._settings.get(["serial", "port"]),
                self._settings.getInt(["serial", "baudrate"]),
            )
            printer_profile = printerProfileManager.get_default()
            connectionOptions = printer.__class__.get_connection_options()
            if port in connectionOptions["ports"] or port == "AUTO" or port is None:
                self._logger.info(f"Trying to connect to configured serial port {port}")
                printer.connect(
                    port=port,
                    baudrate=baudrate,
                    profile=(
                        printer_profile["id"] if "id" in printer_profile else "_default"
                    ),
                )
            else:
                self._logger.info(
                    "Could not find configured serial port {} in the system, cannot automatically connect to a non existing printer. Is it plugged in and booted up yet?"
                )
        except Exception:
            self._logger.exception(
                "Something went wrong while attempting to automatically connect to the printer"
            )

    def _start_serial_autorefresh(self):
        from octoprint.util.comm import serialList

        if not self._settings.getBoolean(["serial", "autorefresh"]):
            return

        last_ports = None
        autorefresh = None

        def refresh_serial_list():
            nonlocal last_ports

            new_ports = sorted(serialList())
            if new_ports != last_ports:
                self._logger.info(
                    "Serial port list was updated, refreshing the port list in the frontend"
                )
                eventManager.fire(
                    events.Events.CONNECTIONS_AUTOREFRESHED,
                    payload={"ports": new_ports},
                )
            last_ports = new_ports

        def autorefresh_active():
            return printer.is_closed_or_error()

        def autorefresh_stopped():
            nonlocal autorefresh

            self._logger.info("Autorefresh of serial port list stopped")
            autorefresh = None

        def run_autorefresh():
            nonlocal autorefresh

            if autorefresh is not None:
                autorefresh.cancel()
                autorefresh = None

            autorefresh = octoprint.util.RepeatedTimer(
                self._settings.getInt(["serial", "autorefreshInterval"]),
                refresh_serial_list,
                run_first=True,
                condition=autorefresh_active,
                on_finish=autorefresh_stopped,
            )
            autorefresh.name = "Serial autorefresh worker"

            self._logger.info("Starting autorefresh of serial port list")
            autorefresh.start()

        run_autorefresh()
        eventManager.subscribe(
            octoprint.events.Events.DISCONNECTED, lambda e, p: run_autorefresh()
        )

    def _start_watched_observer(self):
        try:
            watched = self._settings.getBaseFolder("watched")
            watchdog_handler = util.watchdog.GcodeWatchdogHandler(fileManager, printer)
            watchdog_handler.initial_scan(watched)

            if self._settings.getBoolean(["feature", "pollWatched"]):
                # use less performant polling observer if explicitly configured
                observer = PollingObserver()
            else:
                # use os default
                observer = Observer()

            observer.schedule(watchdog_handler, watched, recursive=True)
            observer.start()
            self._watched_observer = observer
        except Exception:
            self._logger.exception("Error starting watched folder observer")

    def _trigger_after_startup(self):
        from tornado.ioloop import IOLoop

        def on_after_startup():
            if self._host == "::":
                if self._v6_only:
                    # only v6
                    self._logger.info(f"Listening on http://[::]:{self._port}")
                else:
                    # all v4 and v6
                    self._logger.info(
                        "Listening on http://0.0.0.0:{port} and http://[::]:{port}".format(
                            port=self._port
                        )
                    )
            else:
                self._logger.info(
                    "Listening on http://{}:{}".format(
                        self._host if ":" not in self._host else "[" + self._host + "]",
                        self._port,
                    )
                )

            if safe_mode and self._settings.getBoolean(["server", "startOnceInSafeMode"]):
                self._logger.info(
                    "Server started successfully in safe mode as requested from config, removing flag"
                )
                self._settings.setBoolean(["server", "startOnceInSafeMode"], False)
                self._settings.save()

            # now this is somewhat ugly, but the issue is the following: startup plugins might want to do things for
            # which they need the server to be already alive (e.g. for being able to resolve urls, such as favicons
            # or service xmls or the like). While they are working though the ioloop would block. Therefore we'll
            # create a single use thread in which to perform our after-startup-tasks, start that and hand back
            # control to the ioloop
            def work():
                self._call_afterstartup_plugins()

                # if there was a rogue plugin we wouldn't even have made it here, so remove startup triggered safe mode
                # flag again...
                try:
                    incomplete_startup_flag = self._get_incomplete_startup_flag()
                    if incomplete_startup_flag.exists():
                        incomplete_startup_flag.unlink()
                except Exception:
                    self._logger.exception(
                        "Could not clear startup triggered safe mode flag"
                    )

                # make a backup of the current config
                self._settings.backup(ext="backup")

                # when we are through with that we also run our preemptive cache
                if settings().getBoolean(["devel", "cache", "preemptive"]):
                    self._execute_preemptive_flask_caching(preemptiveCache)

            import threading

            threading.Thread(target=work).start()

        IOLoop.current().add_callback(on_after_startup)

    def _register_shutdown_handlers(self):
        from tornado.ioloop import IOLoop

        def on_shutdown():
            # will be called on clean system exit and shutdown the watchdog observer and call the on_shutdown methods
            # on all registered ShutdownPlugins
            self._logger.info("Shutting down...")
            if self._watched_observer:
                self._watched_observer.stop()
                self._watched_observer.join()
            eventManager.fire(events.Events.SHUTDOWN)

            self._call_shutdown_plugins()

            # wait for shutdown event to be processed, but maximally for 15s
            event_timeout = 15.0
            if eventManager.join(timeout=event_timeout):
                self._logger.warning(
                    "Event loop was still busy processing after {}s, shutting down anyhow".format(
                        event_timeout
                    )
                )

            if self._octoprint_daemon is not None:
                self._logger.info("Cleaning up daemon pidfile")
                self._octoprint_daemon.terminated()

            self._logger.info("Goodbye!")

        atexit.register(on_shutdown)

        def sigterm_handler(*args, **kwargs):
            # will stop tornado on SIGTERM, making the program exit cleanly
            def shutdown_tornado():
                self._logger.debug("Shutting down tornado's IOLoop...")
                IOLoop.current().stop()

            self._logger.debug("SIGTERM received...")
            IOLoop.current().add_callback_from_signal(shutdown_tornado)

        signal.signal(signal.SIGTERM, sigterm_handler)

    def _get_incomplete_startup_flag(self):
        return pathlib.Path(self._settings._basedir) / ".incomplete_startup"

    def _call_startup_plugins(self):
        octoprint.plugin.call_plugin(
            octoprint.plugin.StartupPlugin,
            "on_startup",
            args=(self._host, self._port),
            sorting_context="StartupPlugin.on_startup",
        )

        def call_on_startup(name, plugin):
            implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
            if implementation is None:
                return
            implementation.on_startup(self._host, self._port)

        pluginLifecycleManager.add_callback("enabled", call_on_startup)

    def _call_afterstartup_plugins(self):
        octoprint.plugin.call_plugin(
            octoprint.plugin.StartupPlugin,
            "on_after_startup",
            sorting_context="StartupPlugin.on_after_startup",
        )

        def call_on_after_startup(name, plugin):
            implementation = plugin.get_implementation(octoprint.plugin.StartupPlugin)
            if implementation is None:
                return
            implementation.on_after_startup()

        pluginLifecycleManager.add_callback("enabled", call_on_after_startup)

    def _call_shutdown_plugins(self):
        self._logger.info("Calling on_shutdown on plugins")
        octoprint.plugin.call_plugin(
            octoprint.plugin.ShutdownPlugin,
            "on_shutdown",
            sorting_context="ShutdownPlugin.on_shutdown",
        )

    def _start_intermediary_server(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        host = self._host
        port = self._port

        class IntermediaryServerHandler(BaseHTTPRequestHandler):
            def __init__(self, rules=None, *args, **kwargs):
                if rules is None:
                    rules = []
                self.rules = rules
                BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

            def do_GET(self):
                request_path = self.path
                if "?" in request_path:
                    request_path = request_path[0 : request_path.find("?")]

                for rule in self.rules:
                    path, data, content_type = rule
                    if request_path == path:
                        self.send_response(200)
                        if content_type:
                            self.send_header("Content-Type", content_type)
                        self.end_headers()
                        if isinstance(data, str):
                            data = data.encode("utf-8")
                        self.wfile.write(data)
                        break
                else:
                    self.send_response(404)
                    self.wfile.write(b"Not found")

        base_path = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "..", "static")
        )
        rules = [
            (
                "/",
                [
                    "intermediary.html",
                ],
                "text/html",
            ),
            ("/favicon.ico", ["img", "tentacle-20x20.png"], "image/png"),
            (
                "/intermediary.gif",
                bytes(
                    base64.b64decode(
                        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
                    )
                ),
                "image/gif",
            ),
        ]

        def contents(args):
            path = os.path.join(base_path, *args)
            if not os.path.isfile(path):
                return ""

            with open(path, "rb") as f:
                data = f.read()
            return data

        def process(rule):
            if len(rule) == 2:
                path, data = rule
                content_type = None
            else:
                path, data, content_type = rule

            if isinstance(data, (list, tuple)):
                data = contents(data)

            return path, data, content_type

        rules = list(
            map(process, filter(lambda rule: len(rule) == 2 or len(rule) == 3, rules))
        )

        HTTPServerV4 = HTTPServer

        class HTTPServerV6(HTTPServer):
            address_family = socket.AF_INET6

        class HTTPServerV6SingleStack(HTTPServerV6):
            def __init__(self, *args, **kwargs):
                HTTPServerV6.__init__(self, *args, **kwargs)

                # explicitly set V6ONLY flag - seems to be the default, but just to make sure...
                self.socket.setsockopt(
                    octoprint.util.net.IPPROTO_IPV6, octoprint.util.net.IPV6_V6ONLY, 1
                )

        class HTTPServerV6DualStack(HTTPServerV6):
            def __init__(self, *args, **kwargs):
                HTTPServerV6.__init__(self, *args, **kwargs)

                # explicitly unset V6ONLY flag
                self.socket.setsockopt(
                    octoprint.util.net.IPPROTO_IPV6, octoprint.util.net.IPV6_V6ONLY, 0
                )

        if ":" in host:
            # v6
            if host == "::" and not self._v6_only:
                ServerClass = HTTPServerV6DualStack
            else:
                ServerClass = HTTPServerV6SingleStack
        else:
            # v4
            ServerClass = HTTPServerV4

        if host == "::":
            if self._v6_only:
                self._logger.debug(f"Starting intermediary server on http://[::]:{port}")
            else:
                self._logger.debug(
                    "Starting intermediary server on http://0.0.0.0:{port} and http://[::]:{port}".format(
                        port=port
                    )
                )
        else:
            self._logger.debug(
                "Starting intermediary server on http://{}:{}".format(
                    host if ":" not in host else "[" + host + "]", port
                )
            )

        self._intermediary_server = ServerClass(
            (host, port),
            lambda *args, **kwargs: IntermediaryServerHandler(rules, *args, **kwargs),
            bind_and_activate=False,
        )

        # if possible, make sure our socket's port descriptor isn't handed over to subprocesses
        from octoprint.util.platform import set_close_exec

        try:
            set_close_exec(self._intermediary_server.fileno())
        except Exception:
            self._logger.exception(
                "Error while attempting to set_close_exec on intermediary server socket"
            )

        # then bind the server and have it serve our handler until stopped
        try:
            self._intermediary_server.server_bind()
            self._intermediary_server.server_activate()
        except Exception as exc:
            self._intermediary_server.server_close()

            if isinstance(exc, UnicodeDecodeError) and sys.platform == "win32":
                # we end up here if the hostname contains non-ASCII characters due to
                # https://bugs.python.org/issue26227 - tell the user they need
                # to either change their hostname or read up other options in
                # https://github.com/OctoPrint/OctoPrint/issues/3963
                raise CannotStartServerException(
                    "OctoPrint cannot start due to a Python bug "
                    "(https://bugs.python.org/issue26227). Your "
                    "computer's host name contains non-ASCII characters. "
                    "Please either change your computer's host name to "
                    "contain only ASCII characters, or take a look at "
                    "https://github.com/OctoPrint/OctoPrint/issues/3963 for "
                    "other options."
                ) from exc
            else:
                raise

        def serve():
            try:
                self._intermediary_server.serve_forever()
            except Exception:
                self._logger.exception("Error in intermediary server")

        thread = threading.Thread(target=serve)
        thread.daemon = True
        thread.start()

        self._logger.info("Intermediary server started")

    def _stop_intermediary_server(self):
        if self._intermediary_server is None:
            return
        self._logger.info("Shutting down intermediary server...")
        self._intermediary_server.shutdown()
        self._intermediary_server.server_close()
        self._logger.info("Intermediary server shut down")

    def _log_safe_mode_start(self, self_mode):
        self_mode_file = os.path.join(
            self._settings.getBaseFolder("data"), "last_safe_mode"
        )
        try:
            with open(self_mode_file, "w+", encoding="utf-8") as f:
                f.write(self_mode)
        except Exception as ex:
            self._logger.warn(f"Could not write safe mode file {self_mode_file}: {ex}")

    def _create_socket_connection(self, session):
        global \
            printer, \
            fileManager, \
            analysisQueue, \
            userManager, \
            eventManager, \
            connectivityChecker
        return util.sockjs.PrinterStateConnection(
            printer,
            fileManager,
            analysisQueue,
            userManager,
            groupManager,
            eventManager,
            self._plugin_manager,
            connectivityChecker,
            session,
        )

    def _check_for_root(self):
        if "geteuid" in dir(os) and os.geteuid() == 0:
            exit("You should not run OctoPrint as root!")

    def _get_locale(self):
        global LANGUAGES

        l10n = None
        default_language = self._settings.get(["appearance", "defaultLanguage"])

        if "l10n" in request.values:
            # request: query param
            l10n = request.values["l10n"].split(",")

        elif "X-Locale" in request.headers:
            # request: header
            l10n = request.headers["X-Locale"].split(",")

        elif hasattr(g, "identity") and g.identity:
            # user setting
            userid = g.identity.id
            try:
                user_language = userManager.get_user_setting(
                    userid, ("interface", "language")
                )
                if user_language is not None and not user_language == "_default":
                    l10n = [user_language]
            except octoprint.access.users.UnknownUser:
                pass

        if (
            not l10n
            and default_language is not None
            and not default_language == "_default"
            and default_language in LANGUAGES
        ):
            # instance setting
            l10n = [default_language]

        if l10n:
            # canonicalize and get rid of invalid language codes
            l10n_canonicalized = []
            for x in l10n:
                try:
                    l10n_canonicalized.append(str(Locale.parse(x)))
                except Exception:
                    # invalid language code, ignore
                    continue
            return Locale.negotiate(l10n_canonicalized, LANGUAGES)

        # request: preference
        return Locale.parse(request.accept_languages.best_match(LANGUAGES, default="en"))

    def _execute_preemptive_flask_caching(self, preemptive_cache):
        import time

        from werkzeug.test import EnvironBuilder

        # we clean up entries from our preemptive cache settings that haven't been
        # accessed longer than server.preemptiveCache.until days
        preemptive_cache_timeout = settings().getInt(
            ["server", "preemptiveCache", "until"]
        )
        cutoff_timestamp = time.time() - preemptive_cache_timeout * 24 * 60 * 60

        def filter_current_entries(entry):
            """Returns True for entries younger than the cutoff date"""
            return "_timestamp" in entry and entry["_timestamp"] > cutoff_timestamp

        def filter_http_entries(entry):
            """Returns True for entries targeting http or https."""
            return (
                "base_url" in entry
                and entry["base_url"]
                and (
                    entry["base_url"].startswith("http://")
                    or entry["base_url"].startswith("https://")
                )
            )

        def filter_entries(entry):
            """Combined filter."""
            filters = (filter_current_entries, filter_http_entries)
            return all(f(entry) for f in filters)

        # filter out all old and non-http entries
        cache_data = preemptive_cache.clean_all_data(
            lambda root, entries: list(filter(filter_entries, entries))
        )
        if not cache_data:
            return

        def execute_caching():
            logger = logging.getLogger(__name__ + ".preemptive_cache")

            for route in sorted(cache_data.keys(), key=lambda x: (x.count("/"), x)):
                entries = sorted(
                    cache_data[route], key=lambda x: x.get("_count", 0), reverse=True
                )
                for kwargs in entries:
                    plugin = kwargs.get("plugin", None)
                    if plugin:
                        try:
                            plugin_info = self._plugin_manager.get_plugin_info(
                                plugin, require_enabled=True
                            )
                            if plugin_info is None:
                                logger.info(
                                    "About to preemptively cache plugin {} but it is not installed or enabled, preemptive caching makes no sense".format(
                                        plugin
                                    )
                                )
                                continue

                            implementation = plugin_info.implementation
                            if implementation is None or not isinstance(
                                implementation, octoprint.plugin.UiPlugin
                            ):
                                logger.info(
                                    "About to preemptively cache plugin {} but it is not a UiPlugin, preemptive caching makes no sense".format(
                                        plugin
                                    )
                                )
                                continue
                            if not implementation.get_ui_preemptive_caching_enabled():
                                logger.info(
                                    "About to preemptively cache plugin {} but it has disabled preemptive caching".format(
                                        plugin
                                    )
                                )
                                continue
                        except Exception:
                            logger.exception(
                                "Error while trying to check if plugin {} has preemptive caching enabled, skipping entry"
                            )
                            continue

                    additional_request_data = kwargs.get("_additional_request_data", {})
                    kwargs = {
                        k: v
                        for k, v in kwargs.items()
                        if not k.startswith("_") and not k == "plugin"
                    }
                    kwargs.update(additional_request_data)

                    try:
                        start = time.monotonic()
                        if plugin:
                            logger.info(
                                "Preemptively caching {} (ui {}) for {!r}".format(
                                    route, plugin, kwargs
                                )
                            )
                        else:
                            logger.info(
                                "Preemptively caching {} (ui _default) for {!r}".format(
                                    route, kwargs
                                )
                            )

                        builder = EnvironBuilder(**kwargs)
                        environ = builder.get_environ()
                        with app.request_context(environ):
                            g.preemptive_recording_active = True
                            g.preemptive_recording_view = plugin if plugin else "_default"
                            app.full_dispatch_request()

                        logger.info(f"... done in {time.monotonic() - start:.2f}s")
                    except Exception:
                        logger.exception(
                            "Error while trying to preemptively cache {} for {!r}".format(
                                route, kwargs
                            )
                        )

        # asynchronous caching
        import threading

        cache_thread = threading.Thread(
            target=execute_caching, name="Preemptive Cache Worker"
        )
        cache_thread.daemon = True
        cache_thread.start()


class LifecycleManager:
    def __init__(self, plugin_manager):
        self._plugin_manager = plugin_manager

        self._plugin_lifecycle_callbacks = defaultdict(list)
        self._logger = logging.getLogger(__name__)

        def wrap_plugin_event(lifecycle_event, new_handler):
            orig_handler = getattr(self._plugin_manager, "on_plugin_" + lifecycle_event)

            def handler(*args, **kwargs):
                if callable(orig_handler):
                    orig_handler(*args, **kwargs)
                if callable(new_handler):
                    new_handler(*args, **kwargs)

            return handler

        def on_plugin_event_factory(lifecycle_event):
            def on_plugin_event(name, plugin):
                self.on_plugin_event(lifecycle_event, name, plugin)

            return on_plugin_event

        for event in ("loaded", "unloaded", "enabled", "disabled"):
            wrap_plugin_event(event, on_plugin_event_factory(event))

    def on_plugin_event(self, event, name, plugin):
        for lifecycle_callback in self._plugin_lifecycle_callbacks[event]:
            lifecycle_callback(name, plugin)

    def add_callback(self, events, callback):
        if isinstance(events, str):
            events = [events]

        for event in events:
            self._plugin_lifecycle_callbacks[event].append(callback)

    def remove_callback(self, callback, events=None):
        if events is None:
            for event in self._plugin_lifecycle_callbacks:
                if callback in self._plugin_lifecycle_callbacks[event]:
                    self._plugin_lifecycle_callbacks[event].remove(callback)
        else:
            if isinstance(events, str):
                events = [events]

            for event in events:
                if callback in self._plugin_lifecycle_callbacks[event]:
                    self._plugin_lifecycle_callbacks[event].remove(callback)


class CannotStartServerException(Exception):
    pass
