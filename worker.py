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


def _balanced_json(text, start):
    '''Return the substring for the JSON object whose opening brace is at
    ``start``, tracking string/escape state so braces inside string values
    don't throw off the depth count.'''
    depth = 0
    in_str = esc = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None


class Worker(Thread): # Get details

    '''
    Get book details from Ridibooks page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20,
                 query_title=None, search_tags=None):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        # The title calibre searched for. Ridibooks' search returns the series
        # (always volume 1), so the volume the user actually has is taken from
        # this title (e.g. "고귀한 황후-4").
        self.query_title = query_title
        # Clean keyword chips + category from the search result (list of str).
        self.search_tags = search_tags

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
        detail = self._js_object(root, 'bookDetail') or {}

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

        # The JSON-LD/og description is only the truncated preview snippet; the
        # full synopsis lives in bookDetail.description.
        comments = self._clean_description(detail.get('description'))
        if not comments and book_info.get('description'):
            comments = _format_item(book_info['description'])
        if comments:
            mi.comments = comments
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

        self._apply_series(mi, detail, title)

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

    def _js_object(self, root, varname):
        # Ridibooks embeds page data as `var <name> = {...};` inline scripts.
        needle = 'var %s = {' % varname
        for script in root.xpath('//script/text()'):
            i = script.find(needle)
            if i < 0:
                continue
            frag = _balanced_json(script, script.find('{', i))
            if frag:
                try:
                    return json.loads(frag)
                except ValueError:
                    self.log.exception('Failed to parse JS object %r' % varname)
            return None
        return None

    def _series_info(self, detail):
        '''(series_title, series_unit, volume) from the embedded bookDetail,
        or (None, None, None) if this book is not part of a grouped series.'''
        if detail.get('is_series') not in ('1', 1, True):
            return None, None, None
        series_title = detail.get('series_title')
        unit = detail.get('series_unit') or None
        try:
            volume = int(detail.get('volume'))
        except (TypeError, ValueError):
            volume = None
        return (series_title.strip() if series_title else None), unit, volume

    @staticmethod
    def _parse_volume(title):
        '''(volume:int, unit:str|None) from a trailing number in the title, e.g.
        "고귀한 황후-4" -> (4, None); "이제 와 후회해 봤자 6권" -> (6, "권").'''
        if not title:
            return None, None
        m = re.search(r'[\s\-_~]+(\d+)\s*(권|화|부)?\s*$', title)
        if not m:
            return None, None
        return int(m.group(1)), (m.group(2) or None)

    def _apply_series(self, mi, detail, og_title):
        series_title, series_unit, page_volume = self._series_info(detail)
        query_volume, query_unit = self._parse_volume(self.query_title)

        if series_title:
            # The search always lands on volume 1, so prefer the volume number
            # from the title calibre searched for (the user's file name).
            volume = query_volume if query_volume is not None else page_volume
            mi.series = series_title
            if volume is not None:
                mi.series_index = float(volume)
                # Normalise the title to "<series> <N><unit>" to match the
                # user's existing Korean series naming, e.g. "고귀한 황후 4권".
                unit = series_unit or query_unit or '권'
                mi.title = '%s %d%s' % (series_title, volume, unit)
            return

        # Not a grouped series: derive a volume number from the title text.
        if og_title.endswith('권'):
            m = re.search(r'(.*)\s*(\d+)권', og_title)
        else:
            m = re.search(r'(.*)\s*(\d+)화', og_title)
        if m:
            mi.series = m.group(1).strip()
            mi.series_index = float(m.group(2))

    def _clean_description(self, desc):
        if not desc or not desc.strip():
            return None
        # Drop the table-of-contents (chapter list); keep synopsis/author note.
        desc = re.split(r'<(?:b|strong)>\s*&lt;\s*목차\s*&gt;\s*</(?:b|strong)>',
                        desc)[0]
        desc = desc.replace('\r\n', '\n').replace('\r', '\n').strip()
        # Plain-text line breaks -> HTML paragraphs (existing <b>/entities kept).
        paras = [p.replace('\n', '<br/>')
                 for p in re.split(r'\n\s*\n', desc) if p.strip()]
        if not paras:
            return None
        return sanitize_comments_html(''.join('<p>%s</p>' % p for p in paras))

    def parse_tags(self, root, book_info):
        # Ridibooks keyword chips + genre, mapped to calibre tags.
        self.log.info("Parsing tags")

        # Preferred: the clean keyword chips + category from the search result.
        if self.search_tags:
            all_tags = list(self.search_tags)
        else:
            # Fallback (e.g. lookup by id, no search): the page's keyword meta.
            all_tags = self._page_keywords(root, book_info)

        # Always include the genre classification (e.g. the 'BL'/'로판' category).
        genre = book_info.get('genre')
        if isinstance(genre, str) and genre.strip():
            all_tags.append(genre.strip())
        elif isinstance(genre, list):
            all_tags += [g for g in genre if g]

        calibre_tags = self._convert_genres_to_calibre_tags(all_tags)
        if len(calibre_tags) > 0:
            return calibre_tags

    def _page_keywords(self, root, book_info):
        # `<meta name="keywords">` carries the chips plus store/format noise and
        # the author/publisher names; strip the obvious non-tag entries.
        content = root.xpath('//head/meta[@name="keywords"]/@content')
        if not content:
            return []
        names = [unescape(k).strip() for k in content[0].split(',')]
        drop = {'ebook', '전자책', '웹책', '웹소설', '웹툰', '만화', '일반만화'}
        for key in ('author', 'publisher'):
            obj = book_info.get(key)
            if isinstance(obj, dict) and obj.get('name'):
                drop.add(obj['name'].strip())
        return [k for k in names if k and k not in drop and not k.endswith('e북')]

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
