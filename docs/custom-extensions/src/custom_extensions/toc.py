"""
Table of Contents Extension for Python-Markdown
===============================================

See <https://Python-Markdown.github.io/extensions/toc>
for documentation.

Original code Copyright 2008 [Jack Miller](https://codezen.org/)

All changes Copyright 2008-2014 The Python Markdown Project

License: [BSD](https://opensource.org/licenses/bsd-license.php)

"""

import html
import re
import unicodedata
import xml.etree.ElementTree as etree

from markdown.extensions import Extension
from markdown.postprocessors import UnescapePostprocessor
from markdown.treeprocessors import Treeprocessor
from markdown.util import (
    AMP_SUBSTITUTE,
    HTML_PLACEHOLDER_RE,
    AtomicString,
    code_escape,
    parseBoolValue,
)


def slugify(value, separator, unicode=False):
    """Slugify a string, to make it URL friendly."""
    if not unicode:
        # Replace Extended Latin characters with ASCII, i.e. žlutý → zluty
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[{}\s]+".format(separator), separator, value)


def slugify_unicode(value, separator):
    """Slugify a string, to make it URL friendly while preserving Unicode characters."""
    return slugify(value, separator, unicode=True)


IDCOUNT_RE = re.compile(r"^(.*)_([0-9]+)$")


def unique(id, ids):
    """Ensure id is unique in set of ids. Append '_1', '_2'... if not"""
    while id in ids or not id:
        m = IDCOUNT_RE.match(id)
        if m:
            id = "%s_%d" % (m.group(1), int(m.group(2)) + 1)
        else:
            id = "%s_%d" % (id, 1)
    ids.add(id)
    return id


def get_name(el):
    """Get title name."""

    text = []
    for c in el.itertext():
        if isinstance(c, AtomicString):
            text.append(html.unescape(c))
        else:
            text.append(c)
    return "".join(text).strip()


def stashedHTML2text(text, md, strip_entities=True, keep_tags=None):
    """Extract raw HTML from stash, reduce to plain text and swap with placeholder."""

    def _tag_keeper(m):
        return m.group(0) if keep_tags and m.group(1) in keep_tags else ""

    def _html_sub(m):
        """Substitute raw html with plain text."""
        try:
            raw = md.htmlStash.rawHtmlBlocks[int(m.group(1))]
        except (IndexError, TypeError):  # pragma: no cover
            return m.group(0)
        # Strip out tags and/or entities - leaving text
        res = re.sub(r"<\/?\s*([^\s>]+)(\s[^>]*)?>", _tag_keeper, raw)
        if strip_entities:
            res = re.sub(r"(&[\#a-zA-Z0-9]+;)", "", res)
        return res

    return HTML_PLACEHOLDER_RE.sub(_html_sub, text)


def unescape(text):
    """Unescape escaped text."""
    c = UnescapePostprocessor()
    return c.run(text)


def nest_toc_tokens(toc_list):
    """Given an unsorted list with errors and skips, return a nested one.
    [{'level': 1}, {'level': 2}]
    =>
    [{'level': 1, 'children': [{'level': 2, 'children': []}]}]

    A wrong list is also converted:
    [{'level': 2}, {'level': 1}]
    =>
    [{'level': 2, 'children': []}, {'level': 1, 'children': []}]
    """

    ordered_list = []
    if len(toc_list):
        # Initialize everything by processing the first entry
        last = toc_list.pop(0)
        last["children"] = []
        levels = [last["level"]]
        ordered_list.append(last)
        parents = []

        # Walk the rest nesting the entries properly
        while toc_list:
            t = toc_list.pop(0)
            current_level = t["level"]
            t["children"] = []

            # Reduce depth if current level < last item's level
            if current_level < levels[-1]:
                # Pop last level since we know we are less than it
                levels.pop()

                # Pop parents and levels we are less than or equal to
                to_pop = 0
                for p in reversed(parents):
                    if current_level <= p["level"]:
                        to_pop += 1
                    else:  # pragma: no cover
                        break
                if to_pop:
                    levels = levels[:-to_pop]
                    parents = parents[:-to_pop]

                # Note current level as last
                levels.append(current_level)

            # Level is the same, so append to
            # the current parent (if available)
            if current_level == levels[-1]:
                (parents[-1]["children"] if parents else ordered_list).append(t)

            # Current level is > last item's level,
            # So make last item a parent and append current as child
            else:
                last["children"].append(t)
                parents.append(last)
                levels.append(current_level)
            last = t

    return ordered_list


class TocTreeprocessor(Treeprocessor):
    def __init__(self, md, config):
        super().__init__(md)

        self.marker = config["marker"]
        self.title = config["title"]
        self.base_level = int(config["baselevel"]) - 1
        self.slugify = config["slugify"]
        self.sep = config["separator"]
        self.use_anchors = parseBoolValue(config["anchorlink"])
        self.anchorlink_class = config["anchorlink_class"]
        self.use_permalinks = parseBoolValue(config["permalink"], False)
        if self.use_permalinks is None:
            self.use_permalinks = config["permalink"]
        self.permalink_class = config["permalink_class"]
        self.permalink_title = config["permalink_title"]
        self.header_rgx = re.compile("[Hh][123456]")
        if isinstance(config["toc_depth"], str) and "-" in config["toc_depth"]:
            self.toc_top, self.toc_bottom = [
                int(x) for x in config["toc_depth"].split("-")
            ]
        else:
            self.toc_top = 1
            self.toc_bottom = int(config["toc_depth"])
        self.keep_tags = config["keep_tags"]

    def iterparent(self, node):
        """Iterator wrapper to get allowed parent and child all at once."""

        # We do not allow the marker inside a header as that
        # would causes an enless loop of placing a new TOC
        # inside previously generated TOC.
        for child in node:
            if not self.header_rgx.match(child.tag) and child.tag not in [
                "pre",
                "code",
            ]:
                yield node, child
                yield from self.iterparent(child)

    def replace_marker(self, root, elem):
        """Replace marker with elem."""
        for p, c in self.iterparent(root):
            text = "".join(c.itertext()).strip()
            if not text:
                continue

            # To keep the output from screwing up the
            # validation by putting a <div> inside of a <p>
            # we actually replace the <p> in its entirety.

            # The <p> element may contain more than a single text content
            # (nl2br can introduce a <br>). In this situation, c.text returns
            # the very first content, ignore children contents or tail content.
            # len(c) == 0 is here to ensure there is only text in the <p>.
            if c.text and c.text.strip() == self.marker and len(c) == 0:
                for i in range(len(p)):
                    if p[i] == c:
                        p[i] = elem
                        break

    def set_level(self, elem):
        """Adjust header level according to base level."""
        level = int(elem.tag[-1]) + self.base_level
        if level > 6:
            level = 6
        elem.tag = "h%d" % level

    def add_anchor(self, c, elem_id):  # @ReservedAssignment
        anchor = etree.Element("a")
        anchor.text = c.text
        anchor.attrib["href"] = "#" + elem_id
        anchor.attrib["class"] = self.anchorlink_class
        c.text = ""
        for elem in c:
            anchor.append(elem)
        while len(c):
            c.remove(c[0])
        c.append(anchor)

    def add_permalink(self, c, elem_id):
        permalink = etree.Element("a")
        permalink.text = (
            "%spara;" % AMP_SUBSTITUTE
            if self.use_permalinks is True
            else self.use_permalinks
        )
        permalink.attrib["href"] = "#" + elem_id
        permalink.attrib["class"] = self.permalink_class
        if self.permalink_title:
            permalink.attrib["title"] = self.permalink_title
        c.append(permalink)

    def build_toc_div(self, toc_list):
        """Return a string div given a toc list."""
        div = etree.Element("div")
        div.attrib["class"] = "toc"

        # Add title to the div
        if self.title:
            header = etree.SubElement(div, "span")
            header.attrib["class"] = "toctitle"
            header.text = self.title

        def build_etree_ul(toc_list, parent):
            ul = etree.SubElement(parent, "ul")
            for item in toc_list:
                # List item link, to be inserted into the toc div
                li = etree.SubElement(ul, "li")
                link = etree.SubElement(li, "a")
                link.text = item.get("name", "")
                link.attrib["href"] = "#" + item.get("id", "")
                if item["children"]:
                    build_etree_ul(item["children"], li)
            return ul

        build_etree_ul(toc_list, div)

        if "prettify" in self.md.treeprocessors:
            self.md.treeprocessors["prettify"].run(div)

        return div

    def run(self, doc):
        # Get a list of id attributes
        used_ids = set()
        for el in doc.iter():
            if "id" in el.attrib:
                used_ids.add(el.attrib["id"])

        toc_tokens = []
        for el in doc.iter():
            if isinstance(el.tag, str) and self.header_rgx.match(el.tag):
                self.set_level(el)
                text = get_name(el)

                # Do not override pre-existing ids
                if "id" not in el.attrib:
                    innertext = unescape(stashedHTML2text(text, self.md))
                    el.attrib["id"] = unique(
                        self.slugify(innertext, self.sep), used_ids
                    )

                if (
                    int(el.tag[-1]) >= self.toc_top
                    and int(el.tag[-1]) <= self.toc_bottom
                ):
                    toc_tokens.append(
                        {
                            "level": int(el.tag[-1]),
                            "id": el.attrib["id"],
                            "name": unescape(
                                stashedHTML2text(
                                    code_escape(el.attrib.get("data-toc-label", text)),
                                    self.md,
                                    strip_entities=False,
                                    keep_tags=self.keep_tags,
                                )
                            ),
                        }
                    )

                # Remove the data-toc-label attribute as it is no longer needed
                if "data-toc-label" in el.attrib:
                    del el.attrib["data-toc-label"]

                if self.use_anchors:
                    self.add_anchor(el, el.attrib["id"])
                if self.use_permalinks not in [False, None]:
                    self.add_permalink(el, el.attrib["id"])

        toc_tokens = nest_toc_tokens(toc_tokens)
        div = self.build_toc_div(toc_tokens)
        if self.marker:
            self.replace_marker(doc, div)

        # serialize and attach to markdown instance.
        toc = self.md.serializer(div)
        for pp in self.md.postprocessors:
            toc = pp.run(toc)
        self.md.toc_tokens = toc_tokens
        self.md.toc = toc


class TocExtension(Extension):
    TreeProcessorClass = TocTreeprocessor

    def __init__(self, **kwargs):
        self.config = {
            "marker": [
                "[TOC]",
                "Text to find and replace with Table of Contents - "
                'Set to an empty string to disable. Defaults to "[TOC]"',
            ],
            "title": [
                "",
                "Title to insert into TOC <div> - " "Defaults to an empty string",
            ],
            "anchorlink": [
                False,
                "True if header should be a self link - " "Defaults to False",
            ],
            "anchorlink_class": [
                "toclink",
                "CSS class(es) used for the link. " 'Defaults to "toclink"',
            ],
            "permalink": [
                0,
                "True or link text if a Sphinx-style permalink should "
                "be added - Defaults to False",
            ],
            "permalink_class": [
                "headerlink",
                "CSS class(es) used for the link. " 'Defaults to "headerlink"',
            ],
            "permalink_title": [
                "Permanent link",
                "Title attribute of the permalink - " "Defaults to 'Permanent link'",
            ],
            "baselevel": ["1", "Base level for headers."],
            "slugify": [
                slugify,
                "Function to generate anchors based on header text - "
                "Defaults to the headerid ext's slugify function.",
            ],
            "separator": ["-", 'Word separator. Defaults to "-".'],
            "toc_depth": [
                6,
                "Define the range of section levels to include in"
                "the Table of Contents. A single integer (b) defines"
                "the bottom section level (<h1>..<hb>) only."
                "A string consisting of two digits separated by a hyphen"
                'in between ("2-5"), define the top (t) and the'
                "bottom (b) (<ht>..<hb>). Defaults to `6` (bottom).",
            ],
            "keep_tags": [[], "HTML tags to keep in the TOC."],
        }

        super().__init__(**kwargs)

    def extendMarkdown(self, md):
        md.registerExtension(self)
        self.md = md
        self.reset()
        tocext = self.TreeProcessorClass(md, self.getConfigs())
        # Headerid ext is set to '>prettify'. With this set to '_end',
        # it should always come after headerid ext (and honor ids assigned
        # by the header id extension) if both are used. Same goes for
        # attr_list extension. This must come last because we don't want
        # to redefine ids after toc is created. But we do want toc prettified.
        md.treeprocessors.register(tocext, "toc", 5)

    def reset(self):
        self.md.toc = ""
        self.md.toc_tokens = []


def makeExtension(**kwargs):  # pragma: no cover
    return TocExtension(**kwargs)
