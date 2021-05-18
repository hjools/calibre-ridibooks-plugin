#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = 'Jin, Heonkyu <heonkyu.jin@gmail.com>'
__docformat__ = 'restructuredtext en'

import socket, re, datetime
from collections import OrderedDict
from threading import Thread

import lxml.html
import json

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars, unescape
from calibre.utils.localization import canonicalize_lang
from calibre.utils.date import utc_tz

import calibre_plugins.ridibooks.config as cfg
import calibre_plugins.ridibooks.libs.requests as requests

class Worker(Thread): # Get details

    '''
    Get book details from Ridibooks page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()

        lm = {
                'eng': ('English', 'Englisch'),
                'fra': ('French', 'Français'),
                'ita': ('Italian', 'Italiano'),
                'dut': ('Dutch',),
                'deu': ('German', 'Deutsch'),
                'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol'),
                'jpn': ('Japanese', u'日本語'),
                'kor': ('Korean', u'한국어'),
                'por': ('Portuguese', 'Português'),
                }
        self.lang_map = {}
        for code, names in lm.items():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        try:
            self.load_details(self.url, self.timeout)
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def load_details(self, url, timeout):

        def _format_item(str):
            return re.sub('^"(.*)"$', '\\1', unescape(str))

        def _format_list(str):
            return [_.strip() for _ in _format_item(str).split(',')]

        def _find_meta(node, property):
            return [_.get('content') for _ in node if _.get('property') == property][0]

        def _format_date(date_text):
            year = int(date_text[0:4])
            month = int(date_text[4:6]) 
            day = int(date_text[6:])
            return datetime.datetime(year, month, day, tzinfo=utc_tz)

        def _normalize_score(score):
            return float(score)/5.0

        def _get_book_page(self):

            header = {
                'Connection': 'keep-alive',
                'Accept-Language': 'en-us',
                'Host': 'ridibooks.com',
                'Referer': 'https://ridibooks.com/account/login?return_url=https%3A%2F%2Fridibooks.com%2F'
            }
            payload = {
                'user_id': (None, ''),
                'password': (None, ''),
                'cmd': (None, 'login'),
                'return_url': (None, 'https://ridibooks.com/'),
            }
            try:
                with requests.Session() as s:
                    loggedin = s.post('https://ridibooks.com/account/action/login', headers=header, files=payload)
                    response = s.get(url)
                    root = lxml.html.fromstring(response.text)
            except Exception as e:
                self.log.exception(e)
            return root

        root = _get_book_page(self)

        # <meta> tag에서 불러오는 항목
        # 책ID, 제목, ISBN, 이미지URL, 평점
        meta = root.xpath('//head/meta[starts-with(@property, "og") or starts-with(@property, "books")]')

        # schema.org JSON에서 불러오는 항목
        # 제목, 저자, 책소개, 출판사
        ld_json = root.xpath('//head/script[@type="application/ld+json"]/text()')

        ld = [_ for _ in ld_json if '"@type": "Book"' in _][0]
        ld = ld.replace('&lt;', '<')
        ld = ld.replace('&gt;', '>')
        ld = ld.replace('\&quot;', '\\"')
        ld = ld.replace('&quot;', '\\"')
        book_info = json.loads(ld)

        x = url.split("/books/")
        y = x[1].split("?_")
        ridibooks_id = y[0]

        isbn = _find_meta(meta, 'books:isbn')
        cover_url = _find_meta(meta, 'og:image')

        title = _find_meta(meta, 'og:title')
        authors = _format_list(book_info['author']['name'])

        if 'translator' in book_info:
            authors.extend([_ + u'(역자)' for _ in _format_list(book_info['translator']['name'])])

        mi = Metadata(title, authors)
        mi.set_identifier('ridibooks', ridibooks_id)

        mi.cover_url = cover_url
        mi.has_cover = bool(cover_url)

        mi.publisher = _format_item(book_info['publisher']['name'])
        mi.pubdate = _format_date(book_info['datePublished'])

        mi.comments = _format_item(book_info['description'])
        mi.rating = float(_find_meta(meta, 'books:rating:normalized_value'))

        if ridibooks_id:
            if isbn:
                self.plugin.cache_isbn_to_identifier(isbn, ridibooks_id)
            if cover_url:
                self.plugin.cache_identifier_to_cover_url(ridibooks_id, cover_url)

        mi.tags = self.parse_tags(root)

        if title.endswith('권'):
            series = re.search(u'(.*)\s*(\d+)권', title)
        else:
            series = re.search(u'(.*)\s*(\d+)화', title)

        if series:
            mi.series = series.group(1)
            mi.series_index = float(series.group(2))

        mi.language = 'Korean'
        mi.source_relevance = self.relevance

        self.plugin.clean_downloaded_metadata(mi)
        self.result_queue.put(mi)


    def parse_tags(self, root):
        # Goodreads does not have "tags", but it does have Genres (wrapper around popular shelves)
        # We will use those as tags (with a bit of massaging)
        self.log.info("Parsing tags")
        all_tags = list()

        ld_json = root.xpath('//head/script[@type="application/ld+json"]/text()')
        ld = [_ for _ in ld_json if '"@type": "Book"' in _][0]
        ld = ld.replace('&lt;', '<')
        ld = ld.replace('&gt;', '>')
        ld = ld.replace('\&quot;', '\\"')
        ld = ld.replace('&quot;', '\\"')
        book_info = json.loads(ld)
        if 'keywords' in book_info:
            keywords = book_info['keywords']
            keywords = keywords[1:len(keywords)-1]
            keywords_list = keywords.split("\", \"")
            all_tags = keywords_list

        genres_node = root.xpath('//p[@class="info_category_wrap"]')
        if genres_node:
            genre_tags = list()
            added_main_genre = False
            for genre_node in genres_node:
                if not added_main_genre:
                    main_genre_node = genre_node.xpath('a[1]')[0].text_content()
                    genre_tags.append(main_genre_node)
                    added_main_genre = True
                sub_genre_nodes = genre_node.xpath('span[@class="icon-arrow_2_right"]/following-sibling::a[1]')
                genre_tags_list = [sgn.text_content().strip() for sgn in sub_genre_nodes]
                #self.log.info("Found genres_tags list:", genre_tags_list)
                if genre_tags_list:
                    # genre_tags.append(' > '.join(genre_tags_list))
                    genre_tags += genre_tags_list
            if all_tags:
                all_tags += genre_tags
            else:
                all_tags = genre_tags

        calibre_tags = self._convert_genres_to_calibre_tags(all_tags)

        if len(calibre_tags) > 0:
            return calibre_tags

    def _convert_genres_to_calibre_tags(self, genre_tags):
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(),v) for (k,v) in calibre_tag_lookup.items())
        tags_to_add = list()
        for genre_tag in genre_tags:
            tags = calibre_tag_map.get(genre_tag.lower(), None)
            if tags:
                for tag in tags:
                    if tag not in tags_to_add:
                        tags_to_add.append(tag)
            else:
                if genre_tag not in tags_to_add:
                    tags_to_add.append(genre_tag)
        return list(tags_to_add)
