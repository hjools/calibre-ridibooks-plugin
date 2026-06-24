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
from calibre_plugins.ridibooks import open_url

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
            values = [_.get('content') for _ in node if _.get('property') == property]
            return values[0] if values else None

        def _format_date(date_text):
            # Ridibooks now returns ISO 'YYYY-MM-DD'; older data was 'YYYYMMDD'.
            date_text = date_text.strip()
            if '-' in date_text:
                year, month, day = (int(p) for p in date_text.split('-')[:3])
            else:
                year, month, day = (int(date_text[0:4]), int(date_text[4:6]),
                                    int(date_text[6:8]))
            return datetime.datetime(year, month, day, tzinfo=utc_tz)

        def _get_book_page():
            # Public book page; no login needed. urllib (not calibre's mechanize
            # browser, which Ridibooks blocks with HTTP 403).
            raw = open_url(url, timeout=timeout)
            return lxml.html.fromstring(raw)

        root = _get_book_page()

        # <meta> tag에서 불러오는 항목
        # 책ID, 제목, ISBN, 이미지URL, 평점
        meta = root.xpath('//head/meta[starts-with(@property, "og") or starts-with(@property, "books")]')

        # schema.org JSON에서 불러오는 항목
        # 제목, 저자, 책소개, 출판사
        book_info = self._book_ld_json(root)

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
        if book_info.get('datePublished'):
            try:
                mi.pubdate = _format_date(book_info['datePublished'])
            except Exception:
                self.log.exception('Failed to parse datePublished: %r'
                                   % book_info.get('datePublished'))

        mi.comments = _format_item(book_info['description'])
        # 'books:rating:normalized_value' is on a 0..1 scale; calibre wants 0..5.
        rating = _find_meta(meta, 'books:rating:normalized_value')
        if rating is not None:
            mi.rating = max(0.0, min(5.0, float(rating) * 5.0))

        if isbn:
            mi.isbn = isbn
        if ridibooks_id:
            if isbn:
                self.plugin.cache_isbn_to_identifier(isbn, ridibooks_id)
            if cover_url:
                self.plugin.cache_identifier_to_cover_url(ridibooks_id, cover_url)

        mi.tags = self.parse_tags(root, book_info)

        if title.endswith('권'):
            series = re.search(r'(.*)\s*(\d+)권', title)
        else:
            series = re.search(r'(.*)\s*(\d+)화', title)

        if series:
            mi.series = series.group(1).strip()
            mi.series_index = float(series.group(2))

        mi.languages = ['kor']
        mi.source_relevance = self.relevance

        self.plugin.clean_downloaded_metadata(mi)
        self.result_queue.put(mi)


    def _book_ld_json(self, root):
        # Extract and repair the schema.org Book JSON-LD embedded in the page.
        ld_json = root.xpath('//head/script[@type="application/ld+json"]/text()')
        candidates = [_ for _ in ld_json if '"@type": "Book"' in _]
        if not candidates:
            return {}
        ld = candidates[0]
        for entity, char in (('&lt;', '<'), ('&gt;', '>'),
                             (r'\&quot;', '\\"'), ('&quot;', '\\"')):
            ld = ld.replace(entity, char)
        return json.loads(ld)

    def parse_tags(self, root, book_info):
        # Ridibooks genres (and keyword chips) are mapped to calibre tags.
        self.log.info("Parsing tags")
        all_tags = list()

        keywords = book_info.get('keywords')
        if isinstance(keywords, str) and keywords:
            keywords = keywords[1:len(keywords)-1]
            keywords_list = keywords.split("\", \"")
            all_tags = keywords_list

        # The current site exposes the category via the JSON-LD 'genre' field
        # (the old info_category_wrap markup is gone).
        genre = book_info.get('genre')
        if isinstance(genre, str) and genre.strip():
            all_tags.append(genre.strip())
        elif isinstance(genre, list):
            all_tags += [g for g in genre if g]

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
