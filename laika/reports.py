# -*- coding: utf-8 -*-

import os
import imp
import pytz
import json
import datetime as dt
import pandas as pd
import ftplib
import logging
import smtplib
import requests
import shlex
import six
import subprocess
import time

from datetime import datetime
from dateutil.relativedelta import relativedelta, MO
from string import Formatter
from shutil import copyfileobj

from email import encoders
from email.utils import COMMASPACE
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage


class ReportError(Exception):
    """ An exception to denote that report generation failed """


def get_json_credentials(laika_object):
    """
    Returns parsed content of credentials file for a given report or result.
    """
    profile = laika_object.conf['profiles'][laika_object.profile]
    with open(profile['credentials']) as f:
        data = json.load(f)
    return data


def is_buffer(data):
    """ Returns true if data looks like a file-like object. """
    return hasattr(data, 'read') and hasattr(data, 'write')


class BasicReport(object):
    """
    Report base class. All the keyword arguments will be set as object
    attributes to use in subclasses.
    """

    def __init__(self, conf, **kwargs):
        self.conf = conf
        for key, value in kwargs.items():
            setattr(self, key, value)

    def process(self):
        """
        Executes the report process. The logic of the process must be defined
        in subclasses. Must return the data to be used by the result class.
        """
        pass


class ModuleReport(BasicReport):
    """
    Allows to execute custom Report classes.

    This report loads a python file from result_file parameter, instantiates a
    class, defined in result_class parameter, executes process method and
    returns it's result value.
    Instantiated class must be a BasicReport at least.
    """

    def __init__(self, conf, **kwargs):
        self.kwargs = kwargs
        super(ModuleReport, self).__init__(conf, **kwargs)

    def process(self):
        # TODO: rename the parameters from result_.. to report_.. or something
        # alike (or maybe refactor both ModuleResult and ModuleReport)
        module_name = os.path.basename(self.result_file).split('.')[0]
        module = imp.load_source(module_name, self.result_file)
        klass = module.__dict__[self.result_class]
        result = klass(self.conf, **self.kwargs)
        return result.process()


class ReportFormatter(object):
    """
    Formats some string which may contain any key with curly braces notation,
    like this: {now}.
    Dates are always datetimes in isoformat (YYYY-mm-dd HH:MM:SS). It also
    converts it to global/report timezone, if it's present, otherwise all the
    dates are in utc.
    Also, accepts variables argument. Vars are a dictionary with values to
    replace in the template previous to formatting.

    To see complete tutorial on formatting check the documentation.

    Example usage:

    >>> formatter = ReportFormatter()
    >>> formatter.format('{t}')
    '2016-02-10 00:00:00'

    >>> formatter.format('{t-3d}')
    '2016-02-07 00:00:00'

    >>> formatter.format('{t-1m}')
    '2016-01-10 00:00:00'

    Example with variables:
    >>> formatter = ReportFormatter(variables={'foo': 'bar', 'yesterday': '{t-1d}'})
    >>> formatter.format('{foo}')
    'bar'

    >>> formatter.format('{yesterday}')
    '2016-02-09 00:00:00'

    """

    def __init__(self, conf=None, variables=None, **kwargs):
        self.conf = conf
        self.variables = variables

    def _to_format(self, value, fname=None):
        """ Converts the datetime value to isoformatted string. """
        return value.strftime('%Y-%m-%d %H:%M:%S')

    def _apply_variables(self, report_string):
        if self.variables:
            report_string = report_string.format(**self.variables)
        return report_string

    def get_now(self):
        tz = getattr(self, 'timezone', None) or self.conf.timezone
        tz = pytz.timezone(tz) if tz else pytz.utc
        return datetime.now(tz)

    def format(self, report_string):
        """
        Formats the report string. If it contains valid keys, replaces them by
        the respective value. If key does not exists, throws KeyError.
        """
        report_string = self._apply_variables(report_string)
        formatter = Formatter()
        now = self.get_now()
        format_params = {}
        fnames = [fname for _, fname, _, _ in formatter.parse(report_string) if fname]

        time_dict = {'hour': 0, 'minute': 0, 'second': 0, 'microsecond': 0}
        for fname in fnames:
            sep = '-' if '-' in fname else '+'
            splitted = fname.split(sep)
            d = splitted.pop(0).strip()
            result = now
            replacements = None

            if d == 'now':
                pass
            elif d.lower() == 't' or d.lower() == 'd':
                replacements = time_dict
            elif d == 'm':
                replacements = dict(day=1, **time_dict)
            elif d.lower() == 'y':
                replacements = dict(day=1, month=1, **time_dict)
            elif d.lower() == 'h':
                replacements = dict(minute=0, second=0, microsecond=0)
            elif d == 'M':
                replacements = dict(second=0, microsecond=0)
            elif d.lower() == 'w':  # Start of week (Monday)
                start_of_week = now + relativedelta(days=-now.weekday())
                result = datetime.combine(start_of_week.date(), dt.time())
            else:
                continue

            if splitted:
                modifier = splitted[0].strip()
                magnitude = modifier[-1]
                quantity = int(sep + modifier[:-1])
                argument = {
                    'y': 'years',
                    'm': 'months',
                    'd': 'days',
                    'h': 'hours',
                    'M': 'minutes',
                    'w': 'weeks',
                    'f': 'weekday'
                }.get(magnitude, None)
                if magnitude == 'f':
                    # Move the date to a specific relative Monday
                    quantity = MO(quantity)
                result = result + relativedelta(**{argument: quantity})

            if replacements is not None:
                result = result.replace(**replacements)

            format_params[fname] = self._to_format(result, fname)

        return report_string.format(**format_params)


class FilenameFormatter(ReportFormatter):
    """
    Acts like ReportFormatter, but the output date format depends on formatted
    key. For example, if the key is {y}, the date is formatted as '%Y', instead
    of the isoformat.
    """

    def _to_format(self, value, fname=None):
        splitted = fname.split('-' if '-' in fname else '+')
        key = splitted.pop(0).strip()
        f = dict(zip('yYmdDhHM', 'YYmddHHM')).get(key, None)
        return value.strftime('%' + f) if f else value


class FormattedReport(BasicReport):
    """ Report that holds a ReportFormatter instance """

    def __init__(self, conf, *args, **kwargs):
        self.variables = None
        BasicReport.__init__(self, conf, *args, **kwargs)
        self.formatter = ReportFormatter(self.conf, self.variables)


class FileReport(FormattedReport):
    """
    Report that reads from file on path defined by filename parameter.
    Based on extension of given filename, file is parsed as pandas.DataFrame.
    If raw parameter is True, reads the file into a buffer without parsing.
    """

    filename = None
    raw = False
    encoding = 'utf-8'
    extra_args = {}
    converters = {}

    def __init__(self, *args, **kwargs):
        super(FileReport, self).__init__(*args, **kwargs)
        self.file_formatter = FilenameFormatter(self.conf)
        self.filename = self.file_formatter.format(self.filename)
        self.extension = self.filename.split('.')[-1]

    def process_path_or_buff(self, path_or_buf):
        args = dict(encoding=self.encoding)
        args.update(self.extra_args)
        if self.raw:
            if is_buffer(path_or_buf):
                s = six.BytesIO()
                s.write(path_or_buf.read())
                return s
            else:
                return path_or_buf
        if self.extension in {'json'}:
            return pd.read_json(path_or_buf, **args)
        elif self.extension in {'csv'}:
            return pd.read_csv(path_or_buf, **args)
        elif self.extension in {'tsv'}:
            return pd.read_csv(path_or_buf, sep='\t', **args)
        elif self.extension in {'xls', 'xlsx', 'xlsm'}:
            args['converters'] = {k: eval(v) for k, v in self.converters.items()}
            return pd.read_excel(path_or_buf, **args)
        else:
            raise ReportError('Unknown file type! Please, use raw = True.')

    def process(self):
        with open(self.filename) as f:
            return self.process_path_or_buff(f)


class QueryReport(FormattedReport):
    """
    Makes a query to a given sqlalchemy connection.
    The query is supposed to be a sql that the connection understands.
    """

    def __init__(self, *args, **kwargs):
        self.query_file = None
        self.query = None
        super(QueryReport, self).__init__(*args, **kwargs)

        from sqlalchemy import create_engine
        logging.info('Connecting to %s', self.connection)
        constring = self.conf['connections'][self.connection]['constring']
        self.engine = create_engine(constring)

    def process(self):
        with open(self.query_file) as f:
            logging.info('Executing query from %s', self.query_file)
            query = self.formatter.format(f.read())
            df = pd.read_sql_query(query, con=self.engine)
        return df


class RedashReport(FormattedReport):
    """
    Retrieves data from re:dash API. Makes a GET request to the endpoint.
    Needs redash_url, query_id and api_key in order to work (api_key can be
    for query or for user).
    """

    def __init__(self, *args, **kwargs):
        self.api_key = None
        self.redash_url = None
        self.query_id = None
        super(RedashReport, self).__init__(*args, **kwargs)

    def process(self):
        logging.info('Retrieving query %s from %s', self.query_id,
                     self.redash_url)
        path = ('{}/api/queries/{}/results.json?api_key={}'.format(
            self.redash_url, self.query_id, self.api_key))

        response = requests.get(path)
        data = response.json()['query_result']['data']
        return pd.DataFrame(data['rows'])


class BashReport(BasicReport):
    """
    Executes a Bash script and returns it's stdout.
    Script can be defined as inline command via script parameter, or path to
    bash file via script_file parameter.
    If result_type is "json" (default) or "csv" then the output is parsed as
    pandas.DataFrame. If result_type is "raw" then the ouput is returned as
    buffer.
    """
    encoding = 'utf-8'
    result_type = 'json'

    def __init__(self, *args, **kwargs):
        self.script_file = None
        self.script = None
        super(BashReport, self).__init__(*args, **kwargs)

    def process(self):
        command = shlex.split(self.script) if self.script else ['/bin/bash', self.script_file]

        logging.info('Running %s', ' '.join(command))
        p = subprocess.Popen(command, stdout=subprocess.PIPE)

        output = p.communicate()[0]
        if self.result_type == 'json':
            result = pd.read_json(output)
        elif self.result_type == 'csv':
            result = pd.read_csv(output, encoding=self.encoding)
        elif self.result_type == 'raw':
            result = output
        else:
            raise ReportError('Unknown result type: {}!'.format(self.result_type))

        return result


class TrackeameReport(BashReport):
    """
    Runs a bash script that runs a .jar to get data from Trackeame/ClickLAB.

    Requires report_id and report_filename to be defined.
    """
    date = "{Y}-{m}-{d}"
    report_id = None
    report_filename = None

    def __init__(self, *args, **kwargs):
        super(TrackeameReport, self).__init__(*args, **kwargs)

        ff = FilenameFormatter(self.conf)
        self.date = ff.format(self.date)
        self.script = "java -jar {jar_path} -B \"{url}\" -C \"{user}\" \
            -S \"{password}\" -P \"{dir}\" -N \"{report_id}\" -D {date}"
        self.result_type = 'raw'
        self.file_date = self._date_to_file_date()
        self.file_name = self.report_filename.format(date=self.file_date)

        data = get_json_credentials(self)
        self.user_name = data["user_name"]
        self.password = data["password"]
        self.url = data["url"]
        self.script = self.script.format(jar_path=self.jar_path,
                                         url=self.url,
                                         user=self.user_name,
                                         password=self.password,
                                         dir=self.directory,
                                         report_id=self.report_id,
                                         date=self.date
                                         )

    def process(self):
        super(TrackeameReport, self).process()
        df = pd.read_csv(self.directory + "/" + self.file_name, sep='\t',
                         encoding=self.encoding)
        os.remove(self.directory + "/" + self.file_name)
        return df

    def _date_to_file_date(self):
        dto = dt.datetime.strptime(self.date, '%Y-%m-%d')
        file_date = dto - dt.timedelta(days=1)
        return file_date.strftime('%Y-%m-%d')


class AdwordsReport(BasicReport):
    """
    Downloads reports from Google Adwords, using googleads library.

    report_definition parameter is passed directly to ReportDownloader's
    DownloadReport method, so you may want to check google's documentation for
    report definition. As report definitions may be extensive, you can use
    report definition from another adwords report, specifiyng reportName
    parameter.

    A report is downloaded for some customer defined via client_customer_ids
    parameter. If this parameter is a list of customer ids, then results are
    appendend in one report.

    Resulting report is always returned as buffer.
    """
    dateRangeType = None

    adwords_service_version = 'v201806'

    def __init__(self, *args, **kwargs):
        self.report_definition = None
        super(AdwordsReport, self).__init__(*args, **kwargs)

        if self.report_definition is None:
            self.report_definition = self.load_report(self.reportName)

        if not isinstance(self.client_customer_ids, (list, tuple)):
            self.client_customer_ids = [self.client_customer_ids]

        date_range = self.dateRangeType or self.report_definition['dateRangeType']
        self.report_definition['dateRangeType'] = date_range

        from googleads import adwords

        creds_file = self.conf['profiles'][self.profile]['credentials']
        self.ads_client = adwords.AdWordsClient.LoadFromStorage(creds_file)

    def load_report(self, name):
        for key, report in self.conf['reports'].items():
            if report['type'] == 'adwords' and 'report_definition' in report:
                definition = report['report_definition']
                if definition['reportName'] == name:
                    return definition.copy()

    def process(self):
        report_downloader = self.ads_client.GetReportDownloader(version=self.adwords_service_version)
        first_client_id_processed = False
        result = six.BytesIO()
        for customer_id in self.client_customer_ids:
            report_downloader.DownloadReport(
                self.report_definition, result,
                skip_report_header=first_client_id_processed,
                skip_column_header=first_client_id_processed,
                skip_report_summary=True, include_zero_impressions=False,
                client_customer_id=customer_id
            )
            first_client_id_processed = True
        return result


class FacebookInsightsReport(BasicReport):
    """
    Retrieves the data from the insights endpoint of Facebook's graph API.
    More info on Facebook's insights API: https://developers.facebook.com/docs/marketing-api/insights

    Report requieres an object_id, and a set of params to add to the request.
    You can override the defaults via "params" parameter.
    By default, gets the impressions and the reach on the ad level.

    The report is generated via Facebook's API async job, the result of which
    is polled every few seconds, defined via sleep_per_tick parameter.
    """

    defaults = {
        'level': 'ad',
        'limit': 10000000,
        'filtering': '[{"operator": "NOT_IN", "field": "ad.effective_status", "value": ["DELETED"]}]',
        'fields': 'impressions,reach',
        'action_attribution_windows': '28d_click',
        'date_preset': 'last_30d'
    }
    api_version = 'v3.0'
    base_url = 'https://graph.facebook.com/{}/{}'
    endpoint = '/insights'
    job_results_limit = 500
    sleep_per_tick = 60

    def __init__(self, *args, **kwargs):
        self.object_id = None
        super(FacebookInsightsReport, self).__init__(*args, **kwargs)

        self.url = self.base_url + self.endpoint

        d = self.defaults.copy()
        d.update(self.params)
        self.params = d

        self.access_token = get_json_credentials(self)['access_token']

        self.params.update({'access_token': self.access_token})
        logging.getLogger("requests").setLevel(logging.WARNING)

    def wait_for_job_completion(self, job_id):
        tic = 0
        while True:
            # TODO: this loop keeps running forever sometimes. We need to
            # investigate a bit more about this job status response.

            r = requests.get(self.base_url.format(self.api_version, job_id),
                             params={'access_token': self.access_token})
            res = r.json()

            if 'error' in res and res['error']['code'] == 17:
                # User request limit reached, account disabled for 1 minute
                logging.info('Error %s, waiting 61s', res['error']['message'])
                time.sleep(61)
                continue

            status = res['async_status']

            if status == 'Job Completed' and res['async_percent_completion'] == 100:
                break
            elif status == 'Job Not Started' or res['is_running']:
                if tic % 3 == 0:
                    logging.info('Job running, completion percentage: %d', res['async_percent_completion'])
                time.sleep(self.sleep_per_tick)
                tic += 1
                continue
            raise ReportError('Job failed with status: %s', res['async_status'])

    def results_from_response(self, res):
        # TODO: here i'm assuming that the attribution window is not a list
        attr_window = self.params['action_attribution_windows']

        action_breakdown = self.params.get('action_breakdowns', None)

        d = res.json()
        r = []
        for item in d.copy()['data']:
            acts = item.pop('actions', [])
            for act in acts:
                if attr_window in act:
                    if action_breakdown and action_breakdown in act:
                        action_type = act[action_breakdown]
                    else:
                        action_type = act['action_type']
                    item['action.' + action_type] = act[attr_window]
            rs = item.pop('relevance_score', {})
            for k, v in rs.items():
                item['relevance_score.' + k] = v
            r.append(item)
        return r

    def process(self):
        logging.info('Programming report job')
        url = self.url.format(self.api_version, self.object_id)
        r = requests.post(url, params=self.params)

        if 'report_run_id' not in r.json():
            raise ReportError('Could not retrieve the report: %s', r.text)
        report_run_id = r.json()['report_run_id']
        logging.info('Got report job id: %s', report_run_id)

        self.wait_for_job_completion(report_run_id)
        logging.info('Job finished! asking for results')

        params = {'access_token': self.access_token, 'limit': self.job_results_limit,
                  'fields': self.params['fields']}
        url = self.url.format(self.api_version, report_run_id)
        resp = requests.get(url, params=params)
        result = pd.DataFrame(self.results_from_response(resp))

        d = resp.json()
        while 'next' in d['paging']:
            resp = requests.get(d['paging']['next'])
            d = resp.json()
            result = result.append(self.results_from_response(resp), ignore_index=True)

        logging.info('Finished retreiving results')
        return result


class DownloadFromS3(FileReport):
    """
    Downloads an object from Amazon S3. Object's location is defined by bucket
    and filename parameters (filename is the key of the object in S3).

    The resulting object is processed as with FileReport's logic (i.e. parsed
    as pandas.DataFrame if filename's extension is csv-like).
    """

    def __init__(self, *args, **kwargs):
        super(DownloadFromS3, self).__init__(*args, **kwargs)
        import boto3

        self.credentials = get_json_credentials(self)

        key_id = self.credentials['aws_access_key_id']
        logging.info('Connecting to s3 using key %s', key_id)
        self.s3 = boto3.client('s3', **self.credentials)

    def process(self):
        logging.info('Downloading file %s from bucket %s', self.filename, self.bucket)
        obj = self.s3.get_object(Bucket=self.bucket, Key=self.filename)

        return self.process_path_or_buff(obj['Body'])


class Result(object):
    """
    Result baseclass. Every result must inherit from it.

    Kwargs are saved as attributes on every instance.
    """

    def __init__(self, conf, data, **kwargs):
        self.conf = conf
        self.data = data
        for key, value in kwargs.items():
            setattr(self, key, value)

    def save(self):
        """ Saves the data. Must be implemented in subclasses. """
        pass


class ModuleResult(Result):
    """
    Allows to execute custom Result classes.

    This result loads a python file from result_file parameter, instantiates a
    class, defined in result_class parameter, and executes save method.
    Instantiated class must be a Result at least.
    """

    def __init__(self, conf, data, **kwargs):
        self.kwargs = kwargs
        super(ModuleResult, self).__init__(conf, data, **kwargs)

    def save(self):
        module_name = os.path.basename(self.result_file).split('.')[0]
        module = imp.load_source(module_name, self.result_file)
        klass = module.__dict__[self.result_class]
        result = klass(self.conf, self.data, **self.kwargs)
        result.save()


class FileResult(Result):
    """
    Abstract result class for working with files or buffers. Decides how to write
    the file based on it's extension and formats the output filename.
    """

    encoding = 'utf-8'
    filename = 'output.csv'
    index = True
    float_format = None
    header = True

    def __init__(self, *args, **kwargs):
        super(FileResult, self).__init__(*args, **kwargs)
        self.extension = self.filename.split('.')[-1]
        self.file_formatter = FilenameFormatter(self.conf)
        self.raw = not isinstance(self.data, (pd.DataFrame, pd.Panel))

    def get_filename(self):
        """
        Returns formatted filename. Replaces curly braced letters with
        it's equivalent strftime representation.
        """
        return self.file_formatter.format(self.filename)

    def write_data(self, path_or_buf):
        """
        Writes data to a file on given path or to passed buffer. Excel
        formats are written as excel files, tsv as tab separated values and the
        rest as csv.
        """
        args = dict(encoding=self.encoding, index=self.index,
                    float_format=self.float_format, header=self.header)
        if self.extension in {'xls', 'xlsx', 'xlsm'}:
            self.data.to_excel(path_or_buf, engine='xlsxwriter', **args)
        else:
            if self.extension in {'tsv'}:
                args['sep'] = '\t'
            if six.PY2 or isinstance(path_or_buf, six.string_types):
                self.data.to_csv(path_or_buf, **args)
            else:
                # Workaround to pandas not being able to write to BytesIO in Python 3
                s = self.data.to_csv(**args)
                path_or_buf.write(s.encode(self.encoding))

    def get_buffer(self):
        """
        Returns a buffer with file data. Useful for attaching buffer to
        requests, emails, etc. instead of writing to disk.
        """

        if is_buffer(self.data):
            self.data.seek(0)
            return self.data

        io = six.BytesIO()
        if self.raw:
            io.write(self.data)
        elif self.extension in {'xls', 'xlsx', 'xlsm'}:
            writer = pd.ExcelWriter(io, engine='xlsxwriter')
            self.write_data(writer)
            writer.save()
        else:
            self.write_data(io)
        io.seek(0)
        return io


class WriteToFile(FileResult):
    """
    Result that writes the data to a file on given path defined via filename
    parameter.
    """

    def save(self):
        filename = self.get_filename()
        logging.info('Writing result to %s', filename)

        if self.raw:
            mode = 'w+' + ('b' if isinstance(self.data, bytes) else '')
            with open(filename, mode) as f:
                if is_buffer(self.data):
                    self.data.seek(0)
                    copyfileobj(self.data, f)
                else:
                    f.write(self.data)
        else:
            self.write_data(filename)


class SendEmail(FileResult):
    """
    Sends result as attachment to an email.

    Needs a connection of type 'email', and a credentials json file with
    username and password in order to send the email. Also, at least one
    recipient is required. Recipients can be specified as json list.

    Other parameters are filename, user, subject and body. Body can be
    formatted the same way filename does, so you can include dates to body
    template.

    You can add a list of extra messages via extra_text parameter, and add some
    image attachments as buffers. Report result is attached by default, you
    can disable it setting attach_data parameter to False.
    """

    body = 'Report generated {Y}-{m}-{d} {H}:{M}'
    recipients = []
    attachments = []
    extra_text = []
    user = None
    subject = None
    attach_data = True

    def __init__(self, *args, **kwargs):
        super(SendEmail, self).__init__(*args, **kwargs)

        if not self.recipients:
            raise ReportError('Email must have recipients!')

        self.credentials = get_json_credentials(self)

        self.conn_config = self.conf['connections'][self.connection]

    def save(self):
        """
        Sends an email with data attached to it.
        """
        smtp_user = self.credentials['username']
        smtp_pwd = self.credentials['password']
        FROM = self.user or smtp_user
        TO = self.recipients if type(self.recipients) is list else [self.recipients]
        body = self.file_formatter.format(self.body)
        subject = self.file_formatter.format(self.subject)

        message = MIMEMultipart()
        message['Subject'] = subject
        message['From'] = FROM
        message['To'] = COMMASPACE.join(TO)
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        for text in self.extra_text:
            if isinstance(text, six.string_types):
                text = MIMEText(text, 'plain', 'utf-8')
            message.attach(text)

        for attachment in self.attachments:
            fp = open(attachment, 'rb')
            img = MIMEImage(fp.read())
            fp.close()
            img.add_header('Content-ID', '<{}>'.format(attachment))
            message.attach(img)

        logging.info('Email subject: %s, recipients %s', subject, COMMASPACE.join(TO))

        if self.attach_data:
            string_io = self.get_buffer()
            filename = self.get_filename()
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(string_io.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="{}"'.format(filename))
            message.attach(part)

        host, port = self.conn_config['host'], self.conn_config['port']
        try:
            logging.info('Using smtp host: %s', host)
            server = smtplib.SMTP(host, port)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pwd)
            server.sendmail(FROM, TO, message.as_string())
            server.close()
            logging.info('Successfully sent the mail')
        except Exception as e:
            logging.error('Failed to send mail: %s', str(e))


class UploadToFtp(FileResult):
    """
    Uploads the result as binary to a ftp server.

    The file is first uploaded as "buffer.txt", and then renamed into filename.
    """

    def __init__(self, *args, **kwargs):
        super(UploadToFtp, self).__init__(*args, **kwargs)

        self.credentials = get_json_credentials(self)

        self.conn = self.conf['connections'][self.connection]

    def save(self):
        """ Saves the file on the initial directory on ftp server. """
        ftp_user = self.credentials['username']
        ftp_password = self.credentials['password']
        ftp_host = self.conn['host']

        logging.info('Connecting to ftp server: %s', ftp_host)
        ftp = ftplib.FTP(ftp_host)
        ftp.login(ftp_user, ftp_password)

        string_io = self.get_buffer()
        filename = self.get_filename()
        intermediate_name = 'buffer.txt'
        logging.info('Uploading %s', filename)
        ftp.storbinary('STOR %s' % intermediate_name, string_io)
        ftp.rename(intermediate_name, filename)
        ftp.close()

        logging.info('Successfully uploaded the file %s', filename)


class UploadToSftp(FileResult):
    """
    Uploads the result to an SFTP server. Requires credentials to appoint to
    some file with the private key.
    """

    def __init__(self, *args, **kwargs):
        self.private_key = None
        self.username = ''
        self.password = None
        super(UploadToSftp, self).__init__(*args, **kwargs)

        self.credentials = get_json_credentials(self)

        self.username = self.username or self.credentials.get('username', '')
        self.password = self.password or self.credentials.get('password')

        self.conn = self.conf['connections'][self.connection]

    def save(self):
        import paramiko
        logging.info('Connecting to SFTP server: %s', self.conn['host'])

        if 'private_key' in self.credentials:
            pkey_file = self.credentials['private_key']
            rsa_key = paramiko.RSAKey.from_private_key_file(pkey_file)
        else:
            rsa_key = None

        transport = paramiko.Transport((self.conn['host'], self.conn['port']))
        transport.connect(username=self.username, password=self.password, pkey=rsa_key)
        sftp = paramiko.SFTPClient.from_transport(transport)
        filename = self.get_filename()
        logging.info('Uploading %s', filename)
        try:
            sftp.putfo(self.get_buffer(), self.folder + filename)
            logging.info('Successfully uploaded the file  %s', filename)
        finally:
            sftp.close()


def create_drive(profile, grant):
    from httplib2 import Http
    from apiclient import discovery
    from pydrive.auth import GoogleAuth
    from pydrive.drive import GoogleDrive
    from oauth2client.service_account import ServiceAccountCredentials
    """auth google drive, used in both classes"""
    # Authorization method taken from here:
    # http://stackoverflow.com/questions/22555433/pydrive-and-google-drive-automate-verification-process
    logging.info('Authorizing as %s', profile['name'])
    credentials = ServiceAccountCredentials.from_json_keyfile_name(
        profile['credentials'], 'https://www.googleapis.com/auth/drive')
    credentials.authorize(Http())
    credentials = credentials.create_delegated(grant)
    gauth = GoogleAuth()
    # I repeat steps from GoogleAuth.Authorize method
    # https://github.com/googledrive/PyDrive/blob/1.3.1/pydrive/auth.py#L513
    # in order to assign credentials and tweak discovery build parameters
    # (to ignore cache discovery)
    gauth.credentials = credentials
    http = Http(timeout=gauth.http_timeout)
    credentials.authorize(http)
    gauth.service = discovery.build('drive', 'v2', http=http, cache_discovery=False)
    return GoogleDrive(gauth)


class DownloadFromGoogleDrive(FileReport):
    """
    Downloads a file from Google Drive.

    File is specified via file_id or searched by filename. If filename is used,
    it can be combined with folder_id (to search for file in that exact folder),
    folder name (to search first for folder, and then for filename inside), or
    with folder and subfoler (to search for folder/subfolder/filename
    structure).

    The resulting file is converted to pandas.DataFrame or to buffer using
    logic from FileReport.

    Needs a google drive service account credentials in order to download the
    file headlessly.
    """
    folder = None
    folder_id = None
    subfolder = None
    file_id = None
    result_file = None
    mimetype = None

    def __init__(self, *args, **kwargs):
        super(DownloadFromGoogleDrive, self).__init__(*args, **kwargs)
        profile = self.conf['profiles'][self.profile]
        self.drive = create_drive(profile, self.grant)

    def process(self):
        """
        Saves the data from google drive as a dataframe or a file.

        File is retrieved by id or searched by filename.
        If file doesn't exists or the folder is empty it raises an error.
        Mimetype needs to be specified if we want a certain type of file.
        """
        # cleans the result for multiple runs
        self.result_file = None
        parent_file = None

        # if the file id is specified we don't need eveything else
        if self.file_id:
            logging.info('Downloading file by id')
            fd = self.drive.CreateFile({'id': self.file_id})
        else:
            # look for folder and subfolder, if specified.
            parent_file = self.search_folder(self.folder, self.folder_id, None)
            if self.subfolder and not self.folder_id:
                parent_file = self.search_folder(self.subfolder, None, parent_file['id'])

            # if filename is specified, check for existence and download it
            if self.filename:
                query = "trashed=false and title='{}'".format(self.filename)
                if parent_file:
                    query += " and '{}' in parents".format(parent_file['id'])
                file_list = self.drive.ListFile({'q': query, 'maxResults': 1}).GetList()
                if file_list:
                    fd = file_list[0]
                    logging.info('Downloading {} with id: {}'.format(fd['title'], fd['id']))
                else:
                    # File does not exist
                    raise ReportError('File {} not found!'.format(self.filename))
            else:
                raise ReportError('File is not specified!')

        fd.FetchContent(mimetype=self.mimetype)
        self.result_file = fd.content
        return self.process_path_or_buff(self.result_file)

    def search_folder(self, folder=None, folder_id=None, parent_folder_id=None):
        if folder_id:
            parent_file = {'id': folder_id}
        elif folder:
            folder = self.file_formatter.format(folder)
            # If folder is specified, verify it
            logging.info('Checking %s folder', folder)
            query = "trashed=false and title='{}'".format(folder)
            if parent_folder_id:
                query += " and '{}' in parents".format(parent_folder_id)
            file_list = self.drive.ListFile({'q': query, 'maxResults': 1}).GetList()
            if file_list and file_list[0]['mimeType'] == 'application/vnd.google-apps.folder':
                parent_file = file_list[0]
            else:
                raise ReportError('Folder {} not found!'.format(folder))
        else:
            raise ReportError('Folder and Folder id not specified.')

        logging.info("Folder successfully found.")
        return parent_file


class UploadToGoogleDrive(FileResult):
    """
    Uploads the result to Google Drive.

    Needs a google drive service account credentials in order to upload the
    file headlessly. If folder is specified, result is placed inside it. If
    title already exists, it's content is updated.
    """

    folder = None
    folder_id = None
    mime_type = None

    def __init__(self, *args, **kwargs):
        super(UploadToGoogleDrive, self).__init__(*args, **kwargs)
        profile = self.conf['profiles'][self.profile]
        self.drive = create_drive(profile, self.grant)

    def save(self):
        """
        Saves the data to google drive as a google spreadsheet, using filename
        as file's title.
        If folder is specified, the file is placed inside it (first folder with
        given name found), and in the root folder otherwise.
        If the file with given title and inside the given folder exists, it's
        content is updated.
        """
        parent_file, result_file = getattr(self, 'parent_file', None), None
        filename = self.get_filename()

        if not self.folder_id and parent_file is None and self.folder:
            # If folder is specified, verify it
            logging.info('Checking %s folder', self.folder)
            query = "trashed=false and title='{}'".format(self.folder)
            file_list = self.drive.ListFile({'q': query, 'maxResults': 1}).GetList()
            if file_list and file_list[0]['mimeType'] == 'application/vnd.google-apps.folder':
                parent_file = file_list[0]
            else:
                raise ReportError('Folder {} not found!'.format(self.folder))
        elif self.folder_id:
            parent_file = {'id': self.folder_id}

        # Checking if file already exists
        query = "trashed=false and title='{}'".format(filename)
        if parent_file:
            query += " and '{}' in parents".format(parent_file['id'])
        file_list = self.drive.ListFile({'q': query, 'maxResults': 1}).GetList()
        if file_list:
            result_file = file_list[0]
        else:
            # File does not exist, so we create a new one
            # In order to place it in the specified parent folder, it's id is needed
            parents = [{'id': parent_file['id']}] if parent_file else []
            result_file = self.drive.CreateFile({'title': filename,
               'parents': parents, 'mimeType': self.mime_type})

        # Generating an io object with the spreadsheet
        logging.info('Writing the file')

        string_io = self.get_buffer()

        # Uploading the file.
        logging.info('Uploading %s', filename)
        result_file.content = string_io
        result_file.Upload()


class RedashResult(WriteToFile):
    """
    Saves result as json file that can be served to redash via API.
    """

    fillna = ''
    encoding = 'latin-1'

    def __init__(self, *args, **kwargs):
        super(RedashResult, self).__init__(*args, **kwargs)
        self.raw = True

    def save(self):
        self.data.fillna(self.fillna, inplace=True)

        res = {
            'columns': [{'name': c, 'friendly_name': c} for c in self.data.columns],
            'rows': self.data.to_dict('records')
        }

        self.data = six.BytesIO()
        json.dump(res, self.data, encoding=self.encoding, allow_nan=False)
        super(RedashResult, self).save()


class UploadToS3(FileResult):
    """
    Uploads the result to Amazon S3.

    File's destination is defined by bucket and filename parameters (filename
    will be the key of the object in S3).

    The resulting object is processed as with FileReport's logic (i.e.
    converted to excel if extension is xlsx).
    """

    def __init__(self, *args, **kwargs):
        super(UploadToS3, self).__init__(*args, **kwargs)
        import boto3

        self.credentials = get_json_credentials(self)

        key_id = self.credentials['aws_access_key_id']
        logging.info('Connecting to s3 using key %s', key_id)
        self.s3 = boto3.client('s3', **self.credentials)

    def save(self):
        data = self.get_buffer()
        filename = self.get_filename()

        logging.info('Uploading file %s to bucket %s', filename, self.bucket)
        self.s3.put_object(Bucket=self.bucket, Key=filename, Body=data)


class Config(dict):
    """
    Config dict with methods to retrieve some predefined configurations.
    It's actually only saving defined configurations, like timezone, and
    reports, profiles and connections. Those are can be accessed like this:

    >>> conf['reports']['my_report']
    {'type': 'file', 'results': ...}

    Also Config has the mapping for existing Query and Result classes, which
    you can access by name (from configuration file).
    """

    _report_map = {
        'file': FileReport,
        'module': ModuleReport,
        'query': QueryReport,
        'redash': RedashReport,
        'bash': BashReport,
        'adwords': AdwordsReport,
        'facebook': FacebookInsightsReport,
        'trackeame': TrackeameReport,
        'drive': DownloadFromGoogleDrive,
        's3': DownloadFromS3
    }
    _result_map = {
        'module': ModuleResult,
        'file': WriteToFile,
        'email': SendEmail,
        'ftp': UploadToFtp,
        'sftp': UploadToSftp,
        'drive': UploadToGoogleDrive,
        'redash': RedashResult,
        's3': UploadToS3
    }

    def __init__(self, config, pwd=None):
        if isinstance(config, six.string_types):
            config = json.load(open(config))

        self._conf = config
        for key in ['timezone', 'pwd']:
            setattr(self, key, self._conf.get(key, None))

        self.pwd = self.pwd or pwd
        if self.pwd:
            logging.info('Changing directory to %s', self.pwd)
            os.chdir(self.pwd)

        self._update_config(config, self._get_includes(config))

        for key in ['reports', 'profiles', 'connections']:
            self[key] = {i['name']: i for i in self._conf[key]}

    def _get_includes(self, config):
        include_config = {}
        for item in config.get('include', []):
            partial_config = json.load(open(item))
            self._update_config(partial_config, self._get_includes(partial_config))
            self._update_config(include_config, partial_config)
        return include_config

    def _update_config(self, target, source):
        for key in ['reports', 'profiles', 'connections']:
            if key in source:
                if key not in target:
                    target[key] = []
                for item in source[key]:
                    target[key].append(item)

    def get_report_class(self, name):
        """ Returns a report class for a given name if exists, None otherwise """
        return self._report_map.get(name, None)

    def get_result_class(self, name):
        """ Returns a result class for a given name if exists, None otherwise """
        return self._result_map.get(name, None)

    def get_available_reports(self):
        return self['reports'].keys()


class Runner(object):
    """
    Runner is the responsible of running the reports and passing resulting to
    the results, the way they are configured in the given Config instance.
    """

    def __init__(self, conf, **kwargs):
        self.conf = conf
        self.extra_args = kwargs

    def run(self):
        """ Runs every report for a given config """
        for report in self.conf['reports']:
            self.run_report(report)

    def run_report(self, name):
        """ Runs a report for a given report name. """
        logging.info('Running report %s', name)
        report = self.conf['reports'][name]
        if report is None:
            raise ReportError('Report {} not found!'.format(report))
        report_class = self.conf.get_report_class(report['type'])

        if report_class is None:
            raise ReportError('Report type {} does not exist!'.format(report['type']))

        # Validate all the results for this report
        result_configs = []
        for conf in report['results']:
            result_class = self.conf.get_result_class(conf['type'])
            if result_class is None:
                raise ReportError('Result type {} does not exist!'.format(conf['type']))
            result_configs.append((result_class, conf))

        args = {k: v for k, v in report.items() if k not in {'type', 'results'}}
        args.update(self.extra_args)
        data = report_class(self.conf, **args).process()

        if isinstance(data, pd.DataFrame):
            num_rows, num_columns = data.shape
            logging.info('The report has %s rows x %s columns', num_rows, num_columns)

        for result_class, conf in result_configs:
            logging.info('Saving a result of type %s', conf['type'])
            args = {k: v for k, v in conf.items() if k not in {'type'}}
            args.update(self.extra_args)
            result_class(self.conf, data, **args).save()
