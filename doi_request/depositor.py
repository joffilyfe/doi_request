import os
import logging
import logging.config
from datetime import datetime
from io import BytesIO, StringIO

from lxml import etree

from articlemeta.client import ThriftClient
from tasks.celery import register_doi, request_doi_status
from doi_request.models.depositor import Deposit, LogEvent
from doi_request.models import DBSession

logger = logging.getLogger(__name__)

CROSSREF_XSD = open(os.path.dirname(__file__)+'/../xsd/crossref4.4.0.xsd')


class Depositor(object):

    def __init__(self, prefix, api_user, api_key, depositor_name, depositor_email, test_mode=False):

        self._articlemeta = ThriftClient(domain=os.environ.get('ARTICLEMETA_THRIFTSERVER', 'articlemeta.scielo.org:11621'))
        self.prefix = prefix
        self.api_user = api_user
        self.api_key = api_key
        self.depositor_name = depositor_name
        self.depositor_email = depositor_email
        self.test_mode = test_mode
        self._parse_schema()

    def _parse_schema(self):

        try:
            sch_doc = etree.parse(CROSSREF_XSD)
            sch = etree.XMLSchema(sch_doc)
        except Exception as e:
            logger.exception(e)
            logger.error('Fail to parse XML')
            return False

        self.crossref_schema = sch

    def _setup_depositor(self, xml):

        if self.depositor_name:
            registrant = xml.find('//{http://www.crossref.org/schema/4.4.0}registrant')
            registrant.text = self.depositor_name

            depositor_name = xml.find('//{http://www.crossref.org/schema/4.4.0}depositor_name')
            depositor_name.text = self.depositor_name

        if self.depositor_email:
            depositor_email = xml.find('//{http://www.crossref.org/schema/4.4.0}email_address')
            depositor_email.text = self.depositor_email

        return xml

    def xml_is_valid(self, xml):
        xml = BytesIO(xml.encode('utf-8'))
        try:
            xml_doc = etree.parse(xml)
            logger.debug('XML is well formed')
        except Exception as e:
            logger.exception(e)
            logger.error('Fail to parse XML')
            return (False, '', str(e))

        xml_doc = self._setup_depositor(xml_doc)

        try:
            result = self.crossref_schema.assertValid(xml_doc)
            logger.debug('XML is valid')
            return (True, xml_doc, '')
        except etree.DocumentInvalid as e:
            logger.exception(e)
            logger.error('Fail to parse XML')
            return (False, xml_doc, str(e))

    def deposit(self, document):

        code = '_'.join([document.collection_acronym, document.publisher_id])
        log_title = 'Reading document: %s' % code
        logger.info(log_title)
        xml_file_name = '%s.xml' % code
        doi_prefix = document.doi.split('/')[0] if document.doi else ''
        now = datetime.now()
        depitem = Deposit(
            code=code,
            pid=document.publisher_id,
            collection_acronym=document.collection_acronym,
            xml_file_name=xml_file_name,
            doi=document.doi,
            prefix=doi_prefix,
            submission_updated_at=now,
            submission_status='waiting',
            updated_at=now,
            started_at=now
        )

        deposit = DBSession.query(Deposit).filter_by(code=code).first()

        if deposit:
            DBSession.delete(deposit)
            DBSession.commit()

        deposit = DBSession.add(depitem)
        DBSession.commit()

        if doi_prefix.lower() != self.prefix.lower():
            now = datetime.now()
            log_title = 'Document DOI prefix (%s) do no match with the collection prefix (%s)' % (doi_prefix, self.prefix)
            depitem.submission_status = 'notapplicable'
            depitem.feedback_status = 'notapplicable'
            depitem.submission_updated_at = now
            depitem.feedback_updated_at = now
            depitem.updated_at = now
            logevent = LogEvent()
            logevent.title = log_title
            logevent.type = 'general'
            logevent.status = 'notapplicable'
            logevent.deposit_code = depitem.code
            logevent.date = now
            DBSession.add(logevent)
            DBSession.commit()
            return

        try:
            log_title = 'Loading XML document from ArticleMeta (%s)' % code
            logevent = LogEvent()
            logevent.title = log_title
            logevent.type = 'submission'
            logevent.status = 'info'
            logevent.deposit_code = depitem.code
            logevent.date = now
            DBSession.add(logevent)
            xml = self._articlemeta.document(document.publisher_id, document.collection_acronym, fmt='xmlcrossref')
        except Exception as exc:
            logger.exception(exc)
            now = datetime.now()
            log_title = 'Fail to load XML document from ArticleMeta (%s)' % code
            logger.error(log_title)
            depitem.submission_status = 'error'
            depitem.submission_updated_at = now
            depitem.updated_at = now
            logevent = LogEvent()
            logevent.title = log_title
            logevent.body = exc
            logevent.type = 'submission'
            logevent.status = 'error'
            logevent.deposit_code = depitem.code
            logevent.date = now
            DBSession.add(logevent)
            return
        DBSession.commit()

        is_valid, parsed_xml, exc = self.xml_is_valid(xml)
        depitem.submission_xml = etree.tostring(parsed_xml, encoding='utf-8', pretty_print=True).decode('utf-8')

        if is_valid is False:
            log_title = 'XML is invalid, fail to parse xml for document (%s)' % code
            now = datetime.now()
            logger.error(log_title)
            depitem.is_xml_valid = False
            depitem.submission_status = 'error'
            depitem.submission_updated_at = now
            depitem.updated_at = now
            logevent = LogEvent()
            logevent.title = log_title
            logevent.body = exc
            logevent.type = 'submission'
            logevent.status = 'error'
            logevent.deposit_code = depitem.code
            logevent.date = now
            DBSession.add(logevent)
            DBSession.commit()
            return

        log_title = 'XML is valid, it will be submitted to Crossref'
        now = datetime.now()
        logger.info(log_title)
        depitem.is_xml_valid = True
        depitem.submission_status = 'waiting'
        depitem.doi_batch_id = parsed_xml.find('//{http://www.crossref.org/schema/4.4.0}doi_batch_id').text
        logevent = LogEvent()
        logevent.date = datetime.now()
        logevent.title = log_title
        logevent.type = 'submission'
        logevent.status = 'success'
        logevent.deposit_code = depitem.code
        DBSession.add(logevent)
        DBSession.commit()

        # register_doi.apply_async(
        #     (code, xml),
        #     link=request_doi_status.s(doi_batch_id)
        # )

    def deposit_by_pid(self, pid, collection):

        document = self._articlemeta.document(pid, collection)

        self.deposit(document)
