# (C) Copyright 2015 Hewlett Packard Enterprise Development Company LP
"""
This module contains classes which are used to occasionally persist the status
of checks.
"""

# stdlib
import datetime
import logging
import os
import pickle
import platform
import sys
import tempfile
import time

# project
import collections
import config
import util
import yaml

# 3rd party
import ntplib

STATUS_OK = 'OK'
STATUS_ERROR = 'ERROR'
STATUS_WARNING = 'WARNING'

NTP_OFFSET_THRESHOLD = 600


log = logging.getLogger(__name__)


class Stylizer(object):

    STYLES = {
        'bold': 1,
        'grey': 30,
        'red': 31,
        'green': 32,
        'yellow': 33,
        'blue': 34,
        'magenta': 35,
        'cyan': 36,
        'white': 37,
    }

    HEADER = '\033[1m'
    UNDERLINE = '\033[2m'

    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    RESET = '\033[0m'

    ENABLED = False

    @classmethod
    def stylize(cls, text, *styles):
        """stylize the text. """
        if not cls.ENABLED:
            return text
        # don't bother about escaping, not that complicated.
        fmt = '\033[%dm%s'

        for style in styles or []:
            text = fmt % (cls.STYLES[style], text)

        return text + fmt % (0, '')  # reset


# a small convienence method
def style(*args):
    return Stylizer.stylize(*args)


def logger_info():
    loggers = []
    root_logger = logging.getLogger()
    if len(root_logger.handlers) > 0:
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                loggers.append(handler.stream.name)
            if isinstance(handler, logging.handlers.SysLogHandler):
                if isinstance(handler.address, basestring):
                    loggers.append('syslog:%s' % handler.address)
                else:
                    loggers.append('syslog:(%s, %s)' % handler.address)
    else:
        loggers.append("No loggers configured")
    return ', '.join(loggers)


def get_ntp_info():
    ntp_offset = ntplib.NTPClient().request('pool.ntp.org', version=3).offset
    if abs(ntp_offset) > NTP_OFFSET_THRESHOLD:
        ntp_styles = ['red', 'bold']
    else:
        ntp_styles = []
    return ntp_offset, ntp_styles


class AgentStatus(object):
    """A small class used to load and save status messages to the filesystem.
    """

    NAME = None
    agent_config = config.Config()

    def __init__(self):
        self.created_at = datetime.datetime.now()
        self.created_by_pid = os.getpid()

    def has_error(self):
        raise NotImplementedError

    def persist(self):
        try:
            path = self._get_pickle_path()
            log.debug("Persisting status to %s" % path)
            f = open(path, 'w')
            try:
                pickle.dump(self, f)
            finally:
                f.close()
        except Exception:
            log.exception("Error persisting status")

    def created_seconds_ago(self):
        td = datetime.datetime.now() - self.created_at
        return td.seconds

    def render(self):
        indent = "  "
        lines = self._header_lines(indent) + [
            indent + l for l in self.body_lines()
        ] + ["", ""]
        return "\n".join(lines)

    @classmethod
    def _title_lines(cls):
        name_line = "%s (v %s)" % (cls.NAME, AgentStatus.agent_config.get_version())
        lines = [
            "=" * len(name_line),
            "%s" % name_line,
            "=" * len(name_line),
            "",
        ]
        return lines

    def _header_lines(self, indent):
        # Don't indent the header
        lines = self._title_lines()
        if self.created_seconds_ago() > 120:
            styles = ['red', 'bold']
        else:
            styles = []
        # We color it in red if the status is too old
        fields = [
            (
                style("Status date", *styles),
                style("%s (%ss ago)" %
                      (self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                       self.created_seconds_ago()), *styles)
            )
        ]

        fields += [
            ("Pid", self.created_by_pid),
            ("Platform", platform.platform()),
            ("Python Version", platform.python_version()),
            ("Logs", logger_info()),
        ]

        for key, value in fields:
            l = indent + "%s: %s" % (key, value)
            lines.append(l)
        return lines + [""]

    def to_dict(self):
        return {
            'pid': self.created_by_pid,
            'status_date': "%s (%ss ago)" % (self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                                             self.created_seconds_ago())}

    @classmethod
    def _not_running_message(cls):
        lines = cls._title_lines() + [
            style("  %s is not running." % cls.NAME, 'red'),
            style("""  You can get more details in the logs: %s""" % logger_info(), 'red'),
            "",
            ""
        ]
        return "\n".join(lines)

    @classmethod
    def remove_latest_status(cls):
        log.debug("Removing latest status")
        try:
            os.remove(cls._get_pickle_path())
        except OSError:
            pass

    @classmethod
    def load_latest_status(cls):
        try:
            f = open(cls._get_pickle_path())
            try:
                return pickle.load(f)
            finally:
                f.close()
        except IOError:
            log.info("Couldn't load latest status")
            return None

    @classmethod
    def print_latest_status(cls, verbose=False):
        cls.verbose = verbose
        Stylizer.ENABLED = False
        try:
            if sys.stdout.isatty():
                Stylizer.ENABLED = True
        except Exception:
            # Don't worry if we can't enable the
            # stylizer.
            pass

        message = cls._not_running_message()
        exit_code = -1

        module_status = cls.load_latest_status()
        if module_status:
            message = module_status.render()
            exit_code = 0
            if module_status.has_error():
                exit_code = 1

        sys.stdout.write(message)
        return exit_code

    @classmethod
    def _get_pickle_path(cls):
        return os.path.join(tempfile.gettempdir(), cls.__name__ + '.pickle')


class InstanceStatus(object):

    def __init__(self, instance_id, status, error=None, tb=None, warnings=None, metric_count=None):
        self.instance_id = instance_id
        self.status = status
        self.error = repr(error)
        self.traceback = tb
        self.warnings = warnings
        self.metric_count = metric_count

    def has_error(self):
        return self.status == STATUS_ERROR

    def has_warnings(self):
        return self.status == STATUS_WARNING


class CheckStatus(object):

    def __init__(self, check_name, instance_statuses, metric_count,
                 event_count, init_failed_error=None,
                 init_failed_traceback=None, library_versions=None):
        self.name = check_name
        self.instance_statuses = instance_statuses
        self.metric_count = metric_count
        self.event_count = event_count
        self.init_failed_error = init_failed_error
        self.init_failed_traceback = init_failed_traceback
        self.library_versions = library_versions

    @property
    def status(self):
        if self.init_failed_error:
            return STATUS_ERROR
        for instance_status in self.instance_statuses:
            if instance_status.status == STATUS_ERROR:
                return STATUS_ERROR
        return STATUS_OK

    def has_error(self):
        return self.status == STATUS_ERROR


class EmitterStatus(object):

    def __init__(self, name, error=None):
        self.name = name
        self.error = None
        if error:
            self.error = repr(error)

    @property
    def status(self):
        if self.error:
            return STATUS_ERROR
        else:
            return STATUS_OK

    def has_error(self):
        return self.status != STATUS_OK


class CollectorStatus(AgentStatus):

    NAME = 'Collector'

    def __init__(self, check_statuses=None, emitter_statuses=None):
        AgentStatus.__init__(self)
        self.check_statuses = check_statuses or []
        self.emitter_statuses = emitter_statuses or []

    @property
    def status(self):
        for check_status in self.check_statuses:
            if check_status.status == STATUS_ERROR:
                return STATUS_ERROR
        return STATUS_OK

    def has_error(self):
        return self.status != STATUS_OK

    def body_lines(self):
        lines = [
            'Clocks',
            '======',
            ''
        ]
        try:
            ntp_offset, ntp_styles = get_ntp_info()
            lines.append('  ' + style('NTP offset', *ntp_styles) + ': ' +
                         style('%s s' % round(ntp_offset, 4), *ntp_styles))
        except Exception as e:
            lines.append('  NTP offset: Unkwown (%s)' % str(e))
        lines.append('  System UTC time: ' + datetime.datetime.utcnow().__str__())
        lines.append('')

        # Paths to checks_d/conf.d
        lines += [
            'Paths',
            '=====',
            ''
        ]

        paths = util.Paths()
        try:
            confd_path = paths.get_confd_path()
        except util.PathNotFound:
            confd_path = 'Not found'

        try:
            checksd_path = paths.get_checksd_path()
        except util.PathNotFound:
            checksd_path = 'Not found'

        lines.append('  conf.d: ' + confd_path)
        lines.append('  checks_d: ' + checksd_path)
        lines.append('')

        # Hostnames
        lines += [
            'Hostnames',
            '=========',
            ''
        ]

        # Checks.d Status
        lines += [
            'Checks',
            '======',
            ''
        ]
        check_statuses = self.check_statuses + get_jmx_status()
        if not check_statuses:
            lines.append("  No checks have run yet.")
        else:
            for cs in check_statuses:
                check_lines = [
                    '  ' + cs.name,
                    '  ' + '-' * len(cs.name)
                ]
                if cs.init_failed_error:
                    check_lines.append("    - initialize check class [%s]: %s" %
                                       (style(STATUS_ERROR, 'red'),
                                        repr(cs.init_failed_error)))
                    if self.verbose and cs.init_failed_traceback:
                        check_lines.extend('      ' + line for line in
                                           cs.init_failed_traceback.split('\n'))
                else:
                    for s in cs.instance_statuses:
                        c = 'green'
                        if s.has_warnings():
                            c = 'yellow'
                        if s.has_error():
                            c = 'red'
                        line = "    - instance #%s [%s]" % (s.instance_id, style(s.status, c))
                        if s.has_error():
                            line += u": %s" % s.error
                        if s.metric_count is not None:
                            line += " collected %s metrics" % s.metric_count

                        check_lines.append(line)

                        if s.has_warnings():
                            for warning in s.warnings:
                                warn = warning.split('\n')
                                if not len(warn):
                                    continue
                                check_lines.append(u"        %s: %s" %
                                                   (style("Warning", 'yellow'), warn[0]))
                                check_lines.extend(u"        %s" % l for l in warn[1:])
                        if self.verbose and s.traceback is not None:
                            check_lines.extend('      ' + line for line in s.traceback.split('\n'))

                    check_lines += [
                        "    - Collected %s metrics & %s events" % (
                            cs.metric_count, cs.event_count),
                    ]

                    if cs.library_versions is not None:
                        check_lines += [
                            "    - Dependencies:"]
                        for library, version in cs.library_versions.iteritems():
                            check_lines += ["        - %s: %s" % (library, version)]

                    check_lines += [""]

                lines += check_lines

        # Emitter status
        lines += [
            "",
            "Emitters",
            "========",
            ""
        ]
        if not self.emitter_statuses:
            lines.append("  No emitters have run yet.")
        else:
            for es in self.emitter_statuses:
                c = 'green'
                if es.has_error():
                    c = 'red'
                line = "  - %s [%s]" % (es.name, style(es.status, c))
                if es.status != STATUS_OK:
                    line += ": %s" % es.error
                lines.append(line)

        return lines

    def to_dict(self):
        status_info = AgentStatus.to_dict(self)

        # Hostnames
        status_info['hostnames'] = {}

        # Checks.d Status
        status_info['checks'] = {}
        check_statuses = self.check_statuses + get_jmx_status()
        for cs in check_statuses:
            status_info['checks'][cs.name] = {'instances': {}}
            if cs.init_failed_error:
                status_info['checks'][cs.name]['init_failed'] = True
                status_info['checks'][cs.name]['traceback'] = cs.init_failed_traceback
            else:
                status_info['checks'][cs.name] = {'instances': {}}
                status_info['checks'][cs.name]['init_failed'] = False
                for s in cs.instance_statuses:
                    status_info['checks'][cs.name]['instances'][s.instance_id] = {
                        'status': s.status,
                        'has_error': s.has_error(),
                        'has_warnings': s.has_warnings(),
                    }
                    if s.has_error():
                        status_info['checks'][cs.name]['instances'][
                            s.instance_id]['error'] = s.error
                    if s.has_warnings():
                        status_info['checks'][cs.name]['instances'][
                            s.instance_id]['warnings'] = s.warnings
                status_info['checks'][cs.name]['metric_count'] = cs.metric_count
                status_info['checks'][cs.name]['event_count'] = cs.event_count

        # Emitter status
        status_info['emitter'] = []
        for es in self.emitter_statuses:
            check_status = {'name': es.name,
                            'status': es.status,
                            'has_error': es.has_error()}
            if es.has_error():
                check_status['error'] = es.error
            status_info['emitter'].append(check_status)

        paths = util.Paths()
        try:
            status_info['confd_path'] = paths.get_confd_path()
        except config.PathNotFound:
            status_info['confd_path'] = 'Not found'

        try:
            status_info['checksd_path'] = paths.get_checksd_path()
        except config.PathNotFound:
            status_info['checksd_path'] = 'Not found'

        return status_info


class MonascaStatsdStatus(AgentStatus):

    NAME = 'Monasca_Statsd'

    def __init__(self, flush_count=0, packet_count=0,
                 packets_per_second=0, metric_count=0, event_count=0):
        AgentStatus.__init__(self)
        self.flush_count = flush_count
        self.packet_count = packet_count
        self.packets_per_second = packets_per_second
        self.metric_count = metric_count
        self.event_count = event_count

    def has_error(self):
        return self.flush_count == 0 and self.packet_count == 0 and self.metric_count == 0

    def body_lines(self):
        lines = [
            "Flush count: %s" % self.flush_count,
            "Packet Count: %s" % self.packet_count,
            "Packets per second: %s" % self.packets_per_second,
            "Metric count: %s" % self.metric_count,
            "Event count: %s" % self.event_count,
        ]
        return lines

    def to_dict(self):
        status_info = AgentStatus.to_dict(self)
        status_info.update({
            'flush_count': self.flush_count,
            'packet_count': self.packet_count,
            'packets_per_second': self.packets_per_second,
            'metric_count': self.metric_count,
            'event_count': self.event_count,
        })
        return status_info


class ForwarderStatus(AgentStatus):

    NAME = 'Forwarder'

    def __init__(self, queue_length=0, queue_size=0, flush_count=0, transactions_received=0,
                 transactions_flushed=0):
        AgentStatus.__init__(self)
        self.queue_length = queue_length
        self.queue_size = queue_size
        self.flush_count = flush_count
        self.transactions_received = transactions_received
        self.transactions_flushed = transactions_flushed

    def body_lines(self):
        lines = [
            "Queue Size: %s bytes" % self.queue_size,
            "Queue Length: %s" % self.queue_length,
            "Flush Count: %s" % self.flush_count,
            "Transactions received: %s" % self.transactions_received,
            "Transactions flushed: %s" % self.transactions_flushed
        ]
        if self.transactions_flushed == 0:
            lines.append("[%s]: Unable to flush transactions\n           %s" %
                         (style(STATUS_ERROR, 'red'),
                          "Please verify monasca-api is running as configured"))
        elif self.transactions_flushed != self.transactions_received:
            lines.append("[%s]: Transactions out of sync\n             %s" %
                         (style(STATUS_WARNING, 'yellow'),
                          "Likely contact interruption with monasca-api"))
        else:
            lines.append("[%s]: Transactions up to date" %
                         style(STATUS_OK, 'green'))

        return lines

    def has_error(self):
        return self.flush_count == 0

    def to_dict(self):
        status_info = AgentStatus.to_dict(self)
        status_info.update({
            'flush_count': self.flush_count,
            'queue_length': self.queue_length,
            'queue_size': self.queue_size,
        })
        return status_info


def get_jmx_instance_status(instance_name, status, message, metric_count):
    if status == STATUS_ERROR:
        instance_status = InstanceStatus(
            instance_name, STATUS_ERROR, error=message, metric_count=metric_count)

    elif status == STATUS_WARNING:
        instance_status = InstanceStatus(
            instance_name, STATUS_WARNING, warnings=[message], metric_count=metric_count)

    elif status == STATUS_OK:
        instance_status = InstanceStatus(instance_name, STATUS_OK, metric_count=metric_count)

    return instance_status


def get_jmx_status():
    """This function tries to read the 2 jmxfetch status file which are yaml file
    located in the temp directory.

    There are 2 files:
        - One generated by the Agent itself, for jmx checks that can't be initialized because
        there are missing stuff.
        Its format is as following:

        ###
        invalid_checks:
              jmx: !!python/object/apply:jmxfetch.InvalidJMXConfiguration [You need to have at
                              least one instance defined in the YAML file for this check]
        timestamp: 1391040927.136523
        ###

        - One generated by jmxfetch that return information about the collection of metrics
        its format is as following:

        ###
        timestamp: 1391037347435
        checks:
          failed_checks:
            jmx:
            - {message: Unable to create instance. Please check your yaml file, status: ERROR}
          initialized_checks:
            tomcat:
            - {message: null, status: OK, metric_count: 7, instance_name: jmx-remihakim.fr-3000}
        ###
    """
    check_statuses = []
    java_status_path = os.path.join(tempfile.gettempdir(), "jmx_status.yaml")
    python_status_path = os.path.join(tempfile.gettempdir(), "jmx_status_python.yaml")
    if not os.path.exists(java_status_path) and not os.path.exists(python_status_path):
        log.debug("There is no jmx_status file at: %s or at: %s" %
                  (java_status_path, python_status_path))
        return []

    check_data = collections.defaultdict(lambda: collections.defaultdict(list))
    try:
        if os.path.exists(java_status_path):
            java_jmx_stats = yaml.load(file(java_status_path))

            # JMX timestamp is saved in milliseconds
            status_age = time.time() - java_jmx_stats.get('timestamp') / 1000
            jmx_checks = java_jmx_stats.get('checks', {})

            if status_age > 60:
                check_statuses.append(
                    CheckStatus(
                        "jmx", [
                            InstanceStatus(
                                0, STATUS_ERROR, error="JMXfetch didn't return any metrics during the last minute")], 0, 0))
            else:

                for check_name, instances in jmx_checks.get('failed_checks', {}).iteritems():
                    for info in instances:
                        message = info.get('message', None)
                        metric_count = info.get('metric_count', 0)
                        status = info.get('status')
                        instance_name = info.get('instance_name', None)
                        check_data[check_name]['statuses'].append(
                            get_jmx_instance_status(
                                instance_name,
                                status,
                                message,
                                metric_count))
                        check_data[check_name]['metric_count'].append(metric_count)

                for check_name, instances in jmx_checks.get('initialized_checks', {}).iteritems():
                    for info in instances:
                        message = info.get('message', None)
                        metric_count = info.get('metric_count', 0)
                        status = info.get('status')
                        instance_name = info.get('instance_name', None)
                        check_data[check_name]['statuses'].append(
                            get_jmx_instance_status(
                                instance_name,
                                status,
                                message,
                                metric_count))
                        check_data[check_name]['metric_count'].append(metric_count)

                for check_name, data in check_data.iteritems():
                    check_status = CheckStatus(
                        check_name, data['statuses'], sum(data['metric_count']), 0)
                    check_statuses.append(check_status)

        if os.path.exists(python_status_path):
            python_jmx_stats = yaml.load(file(python_status_path))
            jmx_checks = python_jmx_stats.get('invalid_checks', {})
            for check_name, excep in jmx_checks.iteritems():
                check_statuses.append(CheckStatus(check_name, [], 0, 0, init_failed_error=excep))

        return check_statuses

    except Exception:
        log.exception("Couldn't load latest jmx status")
        return []
