#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = 'Helen Lee <ju.helen.lee@gmail.com>'
__docformat__ = 'restructuredtext en'

import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from queue import Queue, Empty

from lxml.html import fromstring, tostring
import json

from calibre import as_unicode
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import Source
from calibre.utils.icu import lower
from calibre.utils.cleantext import clean_ascii_chars

try:
    load_translations()
except NameError:
    pass

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def open_url(url, timeout=30):
    # Ridibooks' WAF returns HTTP 403 to calibre's mechanize browser regardless
    # of User-Agent, but accepts a plain stdlib urllib request with a normal
    # browser User-Agent. Returns the raw response bytes.
    req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
    return urlopen(req, timeout=timeout).read()


# Suffixes Ridibooks appends to category names (e.g. "BL 소설 e북" -> "BL").
_CATEGORY_SUFFIXES = (' 소설 e북', ' e북', ' 웹소설', '웹소설', ' 만화', '만화')


def _clean_category(name):
    if not name:
        return None
    for suffix in _CATEGORY_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    name = name.strip()
    return name or None


class RidiBooks(Source):
    name = 'RidiBooks'
    description = _('Downloads metadata and covers from ridibooks.com')
    author = 'Helen Lee <ju.helen.lee@gmail.com>'
    version = (1, 0, 5)
    minimum_calibre_version = (5, 0, 0)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['title', 'authors', 'identifier:ridibooks',
        'identifier:isbn', 'rating', 'comments', 'publisher', 'pubdate',
        'tags', 'series', 'languages'])
    has_html_comments = True
    supports_gzip_transfer_encoding = True


    BASE_URL = 'https://ridibooks.com'
    SEARCH_URL = 'https://search-api.ridibooks.com/search?site=ridi-store&where=book&where=author&select=n&category_id=0&start=0&what=base&keyword='
    MAX_EDITIONS = 5


    def config_widget(self):
        '''
        Overriding the default configuration screen for our own custom configuration
        '''
        from calibre_plugins.ridibooks.config import ConfigWidget
        return ConfigWidget(self)

    def get_book_url(self, identifiers):
        ridibooks_id = identifiers.get('ridibooks', None)
        if ridibooks_id:
            return ('ridibooks', ridibooks_id,
                    '%s/books/%s' % (RidiBooks.BASE_URL, ridibooks_id))

    def create_query(self, log, title=None, authors=None, identifiers={}):
        isbn = check_isbn(identifiers.get('isbn', None))
        url = ''
        if title or authors:
            title_tokens = list(self.get_title_tokens(title,
                                strip_joiners=False, strip_subtitle=True))
            author_tokens = self.get_author_tokens(authors, only_first_author=True)

            tokens = [quote(t.encode('utf-8') if isinstance(t, str) else t)
                for t in title_tokens]
            tokens += [quote(t.encode('utf-8') if isinstance(t, str) else t)
                for t in author_tokens]
            # url = '/search/?q=' + '+'.join(tokens) + '&adult_exclude=n'
            url = RidiBooks.SEARCH_URL + '+'.join(tokens) + '&adult_exclude=n'

        if not url:
            return None

        log.info('Search from %s' %(url))
        # return RidiBooks.BASE_URL + url
        return url

    def get_cached_cover_url(self, identifiers):
        url = None
        ridibooks_id = identifiers.get('ridibooks', None)
        if ridibooks_id is None:
            isbn = identifiers.get('isbn', None)
            if isbn is not None:
                ridibooks_id = self.cached_isbn_to_identifier(isbn)
        if ridibooks_id is not None:
            url = self.cached_identifier_to_cover_url(ridibooks_id)

        return url

    def identify(self, log, result_queue, abort, title=None, authors=None,
            identifiers={}, timeout=30):
        '''
        Note this method will retry without identifiers automatically if no
        match is found with identifiers.
        '''
        matches = []
        # Unlike the other metadata sources, if we have a goodreads id then we
        # do not need to fire a "search" at Goodreads.com. Instead we will be
        # able to go straight to the URL for that book.
        ridibooks_id = identifiers.get('ridibooks', None)
        isbn = check_isbn(identifiers.get('isbn', None))
        br = self.browser
        if ridibooks_id:
            # (url, tags_info) - no search result here, so no keyword chips.
            matches.append(('%s/books/%s' % (RidiBooks.BASE_URL, ridibooks_id), None))
        else:
            query = self.create_query(log, title=title, authors=authors,
                    identifiers=identifiers)
            if query is None:
                log.error('Insufficient metadata to construct query')
                return
            try:
                log.info('Querying: %s' % query)
                response = open_url(query, timeout=timeout)
                raw = json.loads(response)
            except Exception as e:
                err = 'Failed to make identify query: %r' % query
                log.exception(err)
                return as_unicode(e)

            try:
                # raw = response.read().strip()
                #open('E:\\t.html', 'wb').write(raw)
                # raw = raw.decode('utf-8', errors='replace')
                if not raw:
                    log.error('Failed to get raw result for query: %r' % query)
                    return
                # root = fromstring(clean_ascii_chars(raw))
            except:
                msg = 'Failed to parse ridibooks page for query: %r' % query
                log.exception(msg)
                return msg
            # Now grab the first value from the search results, provided the
            # title and authors appear to be for the same book
            # self._parse_search_results(log, isbn, title, authors, root, matches, timeout)
            self._parse_search_results(log, isbn, title, authors, raw, matches, timeout)

        if abort.is_set():
            return

        if not matches:
            if identifiers and title and authors:
                log.info('No matches found with identifiers, retrying using only'
                        ' title and authors')
                return self.identify(log, result_queue, abort, title=title,
                        authors=authors, timeout=timeout)
            if authors:
                # Ridibooks ANDs keyword terms, and calibre's author tokens can
                # be a partial name that matches nothing. Retry with title only.
                log.info('No matches with title and author, retrying with title only')
                return self.identify(log, result_queue, abort, title=title,
                        authors=None, timeout=timeout)
            log.error('No matches found with query: %r' % query)
            return

        from calibre_plugins.ridibooks.worker import Worker

        workers = [Worker(url, result_queue, br, log, i, self, query_title=title,
                          search_tags=tags)
                for i, (url, tags) in enumerate(matches)]

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None

    def _parse_search_results(self, log, isbn, orig_title, orig_authors, root, matches, timeout):
        search_result = root['book']['books']
        if not search_result:
            return
        title_tokens = list(self.get_title_tokens(orig_title))
        author_tokens = list(self.get_author_tokens(orig_authors, True))

        import difflib
        similarities = []
        for i in range(len(search_result)):
            book = search_result[i]
            title = book['title']
            author = book['author']

            log.info('Compare %s (%s) with %s (%s)' % (title, author, 
                        ' '.join(title_tokens), 
                        ' '.join(author_tokens)))
            title_similarity = difflib.SequenceMatcher(None,
                    title.replace(' ', ''), ''.join(title_tokens)).ratio()
            # Only factor in the author when one was supplied, otherwise the
            # (empty) author comparison zeroes out every score.
            if author_tokens:
                author_similarity = difflib.SequenceMatcher(None,
                        author.replace(' ', ''), ''.join(author_tokens)).ratio()
                similarities.append(title_similarity * author_similarity)
            else:
                similarities.append(title_similarity)

        if not similarities:
            return
        matched_book = search_result[similarities.index(max(similarities))]
        # Clean keyword chips, plus the (cleaned) category names, e.g. "BL".
        search_tags = [t['tag_name'] for t in (matched_book.get('tags_info') or [])
                       if t.get('tag_name')]
        for key in ('category_name', 'parent_category_name',
                    'category_name2', 'parent_category_name2'):
            cat = _clean_category(matched_book.get(key))
            if cat:
                search_tags.append(cat)
        matches.append(('%s/books/%s' % (RidiBooks.BASE_URL, matched_book['b_id']),
                        search_tags))

    def download_cover(self, log, result_queue, abort,
            title=None, authors=None, identifiers={}, timeout=30):
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                    identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        log('Downloading cover from:', cached_url)
        try:
            cdata = open_url(cached_url, timeout=timeout)
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)


if __name__ == '__main__': # tests
    # To run these test use:
    # calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin,
            title_test, authors_test, series_test, tags_test)

    test_identify_plugin(RidiBooks.name, [
        (#테라리움 어드벤처
            {
                'title':u'테라리움 어드벤처 1화'
            },
            [
                title_test(u'테라리움 어드벤처 1화'),
                authors_test([u'수하수하'])
            ]
        ),
        (
            {
                'title':u'오직 네 죽음만이 나를 1권'
            },
            [
                title_test(u'오직 네 죽음만이 나를 1권'),
                authors_test([u'플로나'])
            ]
        ),
        (# 정의란 무엇인가
            {
                'identifiers': {'ridibooks': '593000535'}
            },
            [
                title_test(u'정의란 무엇인가', exact=True),
                authors_test([u'마이클 샌델', u'김명철(역자)'])
            ]
        ),

        (# 세상에서 제일 쉬운 회계학
            {
                'title':u'회계학',
                'authors':[u'구보 유키야']
            },
            [
                title_test(u'세상에서 가장 쉬운 회계학', exact=True),
                authors_test([u'구보 유키야', u'안혜은(역자)'])
            ]
        ),

        (# 테메레르 6권
            {
                'title':u"테메레르 큰바다뱀",
            },
            [
                title_test(u"테메레르 6권 - 큰바다뱀들의 땅", exact=True),
                authors_test([u'나오미 노빅', u'공보경(역자)']),
                series_test(u'테메레르', 6.0)
            ]
        ),

        (# 나홀로 여행 컨설팅북
            {
                'title':u"나홀로 여행 컨설팅북",
            },
            [
                title_test(u"나홀로 여행 컨설팅북", exact=True),
                authors_test([u'이주영']),
            ]
        )
    ], fail_missing_meta=False)


