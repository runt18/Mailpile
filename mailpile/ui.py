#
# This file contains the UserInteraction and Session classes.
#
# The Session encapsulates settings and command results, allowing commands
# to be chanined in an interactive environment.
#
# The UserInteraction classes log the progress and performance of individual
# operations and assist with rendering the results in various formats (text,
# HTML, JSON, etc.).
#
###############################################################################
import datetime
import os
import random
import re
import sys
import tempfile
import traceback
import json
from collections import defaultdict
from gettext import gettext as _
from json import JSONEncoder
from jinja2 import TemplateError, TemplateSyntaxError, TemplateNotFound
from jinja2 import TemplatesNotFound, TemplateAssertionError, UndefinedError

import mailpile.commands
from mailpile.util import *
from mailpile.search import MailIndex


class SuppressHtmlOutput(Exception):
    pass


def default_dict(*args):
    d = defaultdict(str)
    for arg in args:
        d.update(arg)
    return d


class NoColors:
    """Dummy color constants"""
    NORMAL = ''
    BOLD = ''
    NONE = ''
    BLACK = ''
    RED = ''
    YELLOW = ''
    BLUE = ''
    FORMAT = "%s%s"
    RESET = ''

    def color(self, text, color='', weight=''):
        return '%s%s%s' % (self.FORMAT % (color, weight), text, self.RESET)


class ANSIColors(NoColors):
    """ANSI color constants"""
    NORMAL = ''
    BOLD = ';1'
    NONE = '0'
    BLACK = "30"
    RED = "31"
    YELLOW = "33"
    BLUE = "34"
    RESET = "\x1B[0m"
    FORMAT = "\x1B[%s%sm"


class UserInteraction:
    """Log the progress and performance of individual operations"""
    MAX_BUFFER_LEN = 150
    MAX_WIDTH = 79

    LOG_URGENT = 0
    LOG_RESULT = 5
    LOG_ERROR = 10
    LOG_NOTIFY = 20
    LOG_WARNING = 30
    LOG_PROGRESS = 40
    LOG_DEBUG = 50
    LOG_ALL = 99

    def __init__(self, config, log_parent=None):
        self.log_parent = log_parent
        self.log_buffer = []
        self.log_buffering = False
        self.log_level = self.LOG_ALL
        self.interactive = False
        self.time_tracking = [('Main', [])]
        self.time_elapsed = 0.0
        self.last_display = [self.LOG_PROGRESS, 0]
        self.render_mode = 'text'
        self.palette = NoColors()
        self.config = config
        self.html_variables = {
            'title': 'Mailpile',
            'name': 'Chelsea Manning',
            'csrf': '',
            'even_odd': 'odd',
            'mailpile_size': 0
        }

    # Logging
    def _debug_log(self, text, level, prefix=''):
        if 'log' in self.config.sys.debug:
            sys.stderr.write('%slog(%s): %s\n' % (prefix, level, text))

    def _display_log(self, text, level=LOG_URGENT):
        pad = ' ' * max(0, min(self.MAX_WIDTH,
                               self.MAX_WIDTH-len(unicode(text))))
        if self.last_display[0] not in (self.LOG_PROGRESS, ):
            sys.stderr.write('\n')

        c, w, clip = self.palette.NONE, self.palette.NORMAL, 2048
        if level == self.LOG_URGENT:
            c, w = self.palette.RED, self.palette.BOLD
        elif level == self.LOG_ERROR:
            c = self.palette.RED
        elif level == self.LOG_WARNING:
            c = self.palette.YELLOW
        elif level == self.LOG_PROGRESS:
            c, clip = self.palette.BLUE, 78

        sys.stderr.write('%s%s\r' % (
            self.palette.color(unicode(text[:clip]).encode('utf-8'),
                               color=c, weight=w), pad))

        if level == self.LOG_ERROR:
            sys.stderr.write('\n')
        self.last_display = [level, len(unicode(text))]

    def clear_log(self):
        self.log_buffer = []

    def flush_log(self):
        try:
            while len(self.log_buffer) > 0:
                level, message = self.log_buffer.pop(0)
                if level <= self.log_level:
                    self._display_log(message, level)
        except IndexError:
            pass

    def block(self):
        self._display_log('')
        self.log_buffering = True

    def unblock(self):
        self.log_buffering = False
        self.last_display = [self.LOG_RESULT, 0]
        self.flush_log()

    def log(self, level, message):
        if self.log_buffering:
            self.log_buffer.append((level, message))
            while len(self.log_buffer) > self.MAX_BUFFER_LEN:
                self.log_buffer[0:(self.MAX_BUFFER_LEN/10)] = []
        elif level <= self.log_level:
            self._display_log(message, level)

    def finish_command(self):
        pass

    def start_command(self):
        pass

    error = lambda self, msg: self.log(self.LOG_ERROR, msg)
    notify = lambda self, msg: self.log(self.LOG_NOTIFY, msg)
    warning = lambda self, msg: self.log(self.LOG_WARNING, msg)
    progress = lambda self, msg: self.log(self.LOG_PROGRESS, msg)
    debug = lambda self, msg: self.log(self.LOG_DEBUG, msg)

    # Progress indication and performance tracking
    times = property(lambda self: self.time_tracking[-1][1])

    def mark(self, action=None, percent=None):
        """Note that we are about to perform an action."""
        if not action:
            action = self.times and self.times[-1][1] or 'mark'
        self.progress(action)
        self.times.append((time.time(), action))

    def report_marks(self, quiet=False, details=False):
        t = self.times
        if t:
            self.time_elapsed = elapsed = t[-1][0] - t[0][0]
            if not quiet:
                self.notify(_('Elapsed: %.3fs (%s)') % (elapsed, t[-1][1]))
                if True or details:
                    for i in range(0, len(self.times)-1):
                        e = t[i+1][0] - t[i][0]
                        self.notify(' -> %.3fs (%s)' % (e, t[i][1]))
            return elapsed
        return 0

    def reset_marks(self, mark=True, quiet=False, details=False):
        """This sequence of actions is complete."""
        if self.times and mark:
            self.mark()
        elapsed = self.report_marks(quiet=quiet, details=details)
        self.times[:] = []
        return elapsed

    def push_marks(self, subtask):
        """Start tracking a new sub-task."""
        self.time_tracking.append((subtask, []))

    def pop_marks(self, name=None, quiet=True):
        """Sub-task ended!"""
        elapsed = self.report_marks(quiet=quiet)
        if len(self.time_tracking) > 1:
            if not name or (self.time_tracking[-1][0] == name):
                self.time_tracking.pop(-1)
        return elapsed

    # Higher level command-related methods
    def _display_result(self, result):
        sys.stdout.write(unicode(result)+'\n')

    def start_command(self, cmd, args, kwargs):
        self.flush_log()
        self.push_marks(cmd)
        self.mark(('%s(%s)'
                   ) % (cmd, ', '.join((args or tuple()) +
                                       ('%s' % kwargs, ))))

    def finish_command(self, cmd):
        self.pop_marks(name=cmd)

    def display_result(self, result):
        """Render command result objects to the user"""
        self._display_log('', level=self.LOG_RESULT)
        if self.render_mode == 'json':
            return self._display_result(result.as_json())
        for suffix in ('css', 'html', 'js', 'rss', 'txt', 'xml'):
            if self.render_mode.endswith(suffix):
                if self.render_mode in (suffix, 'j' + suffix):
                    template = 'as.' + suffix
                else:
                    template = self.render_mode.replace('.j' + suffix,
                                                        '.' + suffix)
                return self._display_result(
                    result.as_template(suffix, template=template))
        return self._display_result(unicode(result))

    # Creating output files
    DEFAULT_DATA_NAME_FMT = '%(msg_mid)s.%(count)s_%(att_name)s.%(att_ext)s'
    DEFAULT_DATA_ATTRS = {
        'msg_mid': 'file',
        'mimetype': 'application/octet-stream',
        'att_name': 'unnamed',
        'att_ext': 'dat',
        'rand': '0000'
    }
    DEFAULT_DATA_EXTS = {
        # FIXME: Add more!
        'text/plain': 'txt',
        'text/html': 'html',
        'image/gif': 'gif',
        'image/jpeg': 'jpg',
        'image/png': 'png'
    }

    def _make_data_filename(self, name_fmt, attributes):
        return (name_fmt or self.DEFAULT_DATA_NAME_FMT) % attributes

    def _make_data_attributes(self, attributes={}):
        attrs = self.DEFAULT_DATA_ATTRS.copy()
        attrs.update(attributes)
        attrs['rand'] = '%4.4x' % random.randint(0, 0xffff)
        if attrs['att_ext'] == self.DEFAULT_DATA_ATTRS['att_ext']:
            if attrs['mimetype'] in self.DEFAULT_DATA_EXTS:
                attrs['att_ext'] = self.DEFAULT_DATA_EXTS[attrs['mimetype']]
        return attrs

    def open_for_data(self, name_fmt=None, attributes={}):
        filename = self._make_data_filename(
            name_fmt, self._make_data_attributes(attributes))
        return filename, open(filename, 'w')

    # Rendering helpers for templating and such
    def render_json(self, data):
        """Render data as JSON"""
        class NoFailEncoder(JSONEncoder):
            def default(self, obj):
                if isinstance(obj, (list, dict, str, unicode,
                                    int, float, bool, type(None))):
                    return JSONEncoder.default(self, obj)
                return "COMPLEXBLOB"

        return json.dumps(data, indent=1, cls=NoFailEncoder, sort_keys=True)

    def _web_template(self, config, tpl_names, elems=None, ext='html'):
        env = config.jinja_env
        env.session = Session(config)
        env.session.ui = HttpUserInteraction(None, config)
        for tpl_name in tpl_names:
            try:
                fn = '%s.%s' % (tpl_name, ext)
                # FIXME(Security): Here we need to sanitize the file name
                #                  very strictly in case it somehow came
                #                  from user data.
                return env.get_template(fn)
            except (IOError, OSError, AttributeError), e:
                pass
        return None

    def render_web(self, cfg, tpl_names, ext, data):
        """Render data as HTML"""
        alldata = default_dict(self.html_variables)
        alldata["config"] = cfg
        alldata.update(data)
        try:
            template = self._web_template(cfg, tpl_names, ext=ext)
            if template:
                return template.render(alldata)
            else:
                emsg = _("<h1>Template not found</h1>\n<p>%s</p><p>"
                         "<b>DATA:</b> %s</p>")
                tpl_esc_names = [escape_html(tn) for tn in tpl_names]
                return emsg % (' or '.join(tpl_esc_names),
                               escape_html('%s' % alldata))
        except (UndefinedError, ):
            emsg = _("<h1>Template error</h1>\n"
                     "<pre>%s</pre>\n<p>%s</p><p><b>DATA:</b> %s</p>")
            return emsg % (escape_html(traceback.format_exc()),
                           ' or '.join([escape_html(tn) for tn in tpl_names]),
                           escape_html('%.4096s' % alldata))
        except (TemplateNotFound, TemplatesNotFound), e:
            emsg = _("<h1>Template not found in %s</h1>\n"
                     "<b>%s</b><br/>"
                     "<div><hr><p><b>DATA:</b> %s</p></div>")
            return emsg % tuple([escape_html(unicode(v))
                                 for v in (e.name, e.message,
                                           '%.4096s' % alldata)])
        except (TemplateError, TemplateSyntaxError, TemplateAssertionError, ), e:
            emsg = _("<h1>Template error in %s</h1>\n"
                     "Parsing template %s: <b>%s</b> on line %s<br/>"
                     "<div><xmp>%s</xmp><hr><p><b>DATA:</b> %s</p></div>")
            return emsg % tuple([escape_html(unicode(v))
                                 for v in (e.name, e.filename, e.message,
                                           e.lineno, e.source,
                                           '%.4096s' % alldata)])

    def edit_messages(self, session, emails):
        if not self.interactive:
            return False

        sep = '-' * 79 + '\n'
        edit_this = ('\n'+sep).join([e.get_editing_string() for e in emails])

        tf = tempfile.NamedTemporaryFile()
        tf.write(edit_this.encode('utf-8'))
        tf.flush()
        os.system('%s %s' % (os.getenv('VISUAL', default='vi'), tf.name))
        tf.seek(0, 0)
        edited = tf.read().decode('utf-8')
        tf.close()

        if edited == edit_this:
            return False

        updates = [t.strip() for t in edited.split(sep)]
        if len(updates) != len(emails):
            raise ValueError(_('Number of edit messages does not match!'))
        for i in range(0, len(updates)):
            emails[i].update_from_string(session, updates[i])
        return True


class HttpUserInteraction(UserInteraction):
    def __init__(self, request, *args, **kwargs):
        UserInteraction.__init__(self, *args, **kwargs)
        self.request = request
        self.logged = []
        self.results = []

    # Just buffer up rendered data
    def _display_log(self, text, level=UserInteraction.LOG_URGENT):
        self._debug_log(text, level, prefix='http/')
        self.logged.append((level, text))

    def _display_result(self, result):
        self.results.append(result)

    # Stream raw data to the client on open_for_data
    def open_for_data(self, name_fmt=None, attributes={}):
        return 'HTTP Client', RawHttpResponder(self.request, attributes)

    # Render to HTML/JSON/...
    def _render_jembed_response(self, config):
        return json.dumps(default_dict(self.html_variables, {
            'results': self.results,
            'logged': self.logged,
        }))

    def _render_text_responses(self, config):
        if config.sys.debug:
            return '%s\n%s' % (
                '\n'.join([l[1] for l in self.logged]),
                ('\n%s\n' % ('=' * 79)).join(self.results)
            )
        else:
            return ('\n%s\n' % ('=' * 79)).join(self.results)

    def _render_single_response(self, config):
        if len(self.results) == 1:
            return self.results[0]
        if len(self.results) > 1:
            raise Exception(_('FIXME: Multiple results, OMG WTF'))
        return ""

    def render_response(self, config):
        if self.render_mode == 'json':
            if len(self.results) == 1:
                return ('application/json', self.results[0])
            else:
                return ('application/json', '[%s]' % ','.join(self.results))
        elif self.render_mode.split('.')[-1] in ('jcss', 'jhtml', 'jjs',
                                                 'jrss', 'jtxt', 'jxml'):
            return ('application/json', self._render_jembed_response(config))
        elif self.render_mode.endswith('html'):
            return ('text/html', self._render_single_response(config))
        elif self.render_mode.endswith('js'):
            return ('text/javascript', self._render_single_response(config))
        elif self.render_mode.endswith('css'):
            return ('text/css', self._render_single_response(config))
        elif self.render_mode.endswith('txt'):
            return ('text/plain', self._render_single_response(config))
        elif self.render_mode.endswith('rss'):
            return ('application/rss+xml',
                    self._render_single_response(config))
        elif self.render_mode.endswith('xml'):
            return ('application/xml', self._render_single_response(config))
        else:
            return ('text/plain', self._render_text_responses(config))

    def edit_messages(self, session, emails):
        return False

    def print_filters(self, args):
        print args
        return args


class BackgroundInteraction(UserInteraction):
    def _display_log(self, text, level=UserInteraction.LOG_URGENT):
        self._debug_log(text, level, prefix='bg/')

    def edit_messages(self, session, emails):
        return False


class SilentInteraction(UserInteraction):
    def _display_log(self, text, level=UserInteraction.LOG_URGENT):
        self._debug_log(text, level, prefix='silent/')

    def _display_result(self, result):
        return result

    def edit_messages(self, session, emails):
        return False


class RawHttpResponder:

    def __init__(self, request, attributes={}):
        self.raised = False
        self.request = request
        #
        # FIXME: Security risks here, untrusted content may find its way into
        #                our raw HTTP headers etc.
        #
        mimetype = attributes.get('mimetype', 'application/octet-stream')
        filename = attributes.get('filename', 'attachment.dat'
                                  ).replace('"', '')
        disposition = attributes.get('disposition', 'attachment')
        length = attributes['length']
        request.send_http_response(200, 'OK')
        headers = [
            ('Content-Length', length),
        ]
        if disposition and filename:
            headers.append(('Content-Disposition',
                            '%s; filename="%s"' % (disposition, filename)))
        elif disposition:
            headers.append(('Content-Disposition', disposition))
        request.send_standard_headers(header_list=headers,
                                      mimetype=mimetype)

    def write(self, data):
        self.request.wfile.write(data)

    def close(self):
        if not self.raised:
            self.raised = True
            raise SuppressHtmlOutput()


class Session(object):

    def __init__(self, config):
        self.config = config
        self.interactive = False
        self.main = False
        self.order = None
        self.wait_lock = threading.Condition()
        self.results = []
        self.searched = []
        self.displayed = (0, 0)
        self.task_results = []
        self.ui = UserInteraction(config)

    def report_task_completed(self, name, result):
        self.wait_lock.acquire()
        self.task_results.append((name, result))
        self.wait_lock.notify_all()
        self.wait_lock.release()

    def report_task_failed(self, name):
        self.report_task_completed(name, None)

    def wait_for_task(self, wait_for, quiet=False):
        while True:
            self.wait_lock.acquire()
            for i in range(0, len(self.task_results)):
                if self.task_results[i][0] == wait_for:
                    tn, rv = self.task_results.pop(i)
                    self.wait_lock.release()
                    self.ui.reset_marks(quiet=quiet)
                    return rv

            self.wait_lock.wait()
            self.wait_lock.release()

    def error(self, message):
        self.ui.error(message)
        if not self.interactive:
            sys.exit(1)
