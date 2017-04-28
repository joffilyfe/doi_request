# coding: utf-8
"""
Este processamento realiza a exportação de registros SciELO para o Crossref
"""
import os
import argparse
import logging
import logging.config
import codecs
import json
from datetime import datetime, timedelta

from articlemeta.client import ThriftClient
from pyramid.config import Configurator
from sqlalchemy import create_engine

from doi_request.models import initialize_sql
from doi_request.controller import Depositor
from processing import utils

logger = logging.getLogger(__name__)

FROM = datetime.now() - timedelta(days=30)
FROM = FROM.isoformat()[:10]
UNTIL = datetime.now().isoformat()[:10]

SENTRY_HANDLER = os.environ.get('SENTRY_HANDLER', None)
LOGGING_LEVEL = os.environ.get('LOGGING_LEVEL', 'DEBUG')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,

    'formatters': {
        'console': {
            'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            'datefmt': '%H:%M:%S',
            },
        },
    'handlers': {
        'console': {
            'level': LOGGING_LEVEL,
            'class': 'logging.StreamHandler',
            'formatter': 'console'
            }
        },
    'loggers': {
        '': {
            'handlers': ['console'],
            'level': LOGGING_LEVEL,
            'propagate': False,
        },
        'processing.exportDOI': {
            'level': LOGGING_LEVEL,
            'propagate': True,
        },
        'doi_request': {
            'level': LOGGING_LEVEL,
            'propagate': True,
        },
    }
}

if SENTRY_HANDLER:
    LOGGING['handlers']['sentry'] = {
        'level': 'ERROR',
        'class': 'raven.handlers.logging.SentryHandler',
        'dsn': SENTRY_HANDLER,
    }
    LOGGING['loggers']['']['handlers'].append('sentry')

# Database Config
engine = create_engine(os.environ.get('SQL_ENGINE', 'sqlite:///:memory:'))
initialize_sql(engine)


class ExportDOI(object):

    def __init__(self, collection, issns=None, output_file=None, from_date=FROM,
                 until_date=UNTIL, prefix=None, api_user=None, api_key=None,
                 depositor_name=None, depositor_email=None, test_mode=False):

        self._articlemeta = ThriftClient(domain=os.environ.get('ARTICLEMETA_THRIFTSERVER', 'articlemeta.scielo.org:11621'))
        self._depositor = Depositor(prefix=prefix, api_user=api_user, api_key=api_key, depositor_name=depositor_name, depositor_email=depositor_email, test_mode=test_mode)
        self.collection = collection
        self.from_date = from_date
        self.until_date = until_date
        self.issns = issns or [None]

    def run(self):
        logger.info('Processing Started')
        logger.info('Date range (%s) to (%s)',  self.from_date, self.until_date)

        count = 0
        for issn in self.issns:
            for document in self._articlemeta.documents(
                    collection=self.collection, issn=issn,
                    from_date=self.from_date, until_date=self.until_date):

                self._depositor.deposit(document)

        logger.info('Processing Finished')


def main():

    parser = argparse.ArgumentParser(
        description='Load documents into DOAJ'
    )

    parser.add_argument(
        'issns',
        nargs='*',
        help='ISSN\'s separated by spaces'
    )

    parser.add_argument(
        '--issns_file',
        '-i',
        default=None,
        help='Full path to a txt file within a list of ISSNs to be exported'
    )

    parser.add_argument(
        '--api_user',
        '-u',
        default=os.environ.get('CROSSREF_API_USER', None),
        help='Crossref Publisher account name'
    )

    parser.add_argument(
        '--api_key',
        '-p',
        default=os.environ.get('CROSSREF_API_PASSWORD', None),
        help='Crossref Publisher account password'
    )

    parser.add_argument(
        '--prefix',
        '-x',
        default=os.environ.get('CROSSREF_PREFIX', None),
        help='Crossref Prefix'
    )

    parser.add_argument(
        '--depositor_name',
        '-d',
        default=os.environ.get('CROSSREF_DEPOSITOR_NAME', None),
        help='Name of the depositor. It will be attached to the XML metadata'
    )

    parser.add_argument(
        '--depositor_email',
        '-e',
        default=os.environ.get('CROSSREF_DEPOSITOR_EMAIL', None),
        help='Email of the depositor. It will be attached to the XML metadata'
    )

    parser.add_argument(
        '--test_mode',
        '-t',
        action='store_true',
        help='Set submission mode to Test Mode. It will use the Crossref Testing API '
    )

    parser.add_argument(
        '--collection',
        '-c',
        help='Collection Acronym'
    )

    parser.add_argument(
        '--from_date',
        '-f',
        default=FROM,
        help='ISO date like %s' % FROM
    )

    parser.add_argument(
        '--until_date',
        '-g',
        default=UNTIL,
        help='ISO date like %s' % UNTIL
    )

    parser.add_argument(
        '--date_range',
        '-r',
        type=int,
        help='Days from the current date to setup the processing date range. It will overwrite any definition of the arguments from_date and until_date'
    )

    parser.add_argument(
        '--logging_level',
        '-l',
        default=LOGGING_LEVEL,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Logging level'
    )

    args = parser.parse_args()
    LOGGING['handlers']['console']['level'] = args.logging_level
    for content in LOGGING['loggers'].values():
        content['level'] = args.logging_level
    logging.config.dictConfig(LOGGING)

    logger.info('Dumping data for: %s' % args.collection)

    issns = None
    if len(args.issns) > 0:
        issns = utils.ckeck_given_issns(args.issns)

    issns_from_file = None
    if args.issns_file:
        with open(args.issns_file, 'r') as f:
            issns_from_file = utils.ckeck_given_issns([i.strip() for i in f])

    if issns:
        issns += issns_from_file if issns_from_file else []
    else:
        issns = issns_from_file if issns_from_file else []

    if args.date_range:
        from_date = datetime.now() - timedelta(days=args.date_range)
        args.from_date = from_date.isoformat()[:10]
        args.until_date = datetime.now().isoformat()[:10]

    export = ExportDOI(
        args.collection, issns, from_date=args.from_date, until_date=args.until_date,
        prefix=args.prefix, api_user=args.api_user, api_key=args.api_key,
        depositor_name=args.depositor_name, depositor_email=args.depositor_email,
        test_mode=args.test_mode)

    export.run()
