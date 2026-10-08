"""Word, PowerPoint and RTF extraction: every text container is read, none twice."""

from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import patch

from canvas_reader import extract
from canvas_reader.extract import DocumentError, document_kind, extract_document, rtf_text

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "p188": "http://schemas.microsoft.com/office/powerpoint/2018/8/main",
}
DECL = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items())
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def rels(*entries) -> str:
    items = []
    for rid, kind, target, *external in entries:
        mode = ' TargetMode="External"' if external else ""
        rel_type = kind if kind.startswith("http") else REL + kind
        items.append(f'<Relationship Id="{rid}" Type="{rel_type}" Target="{target}"{mode}/>')
    return ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(items) + "</Relationships>")


def package(files: dict[str, str | bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


def run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def para(text: str) -> str:
    return f"<w:p>{run(text)}</w:p>"


def docx(body: str, document_rels=(), parts=None, sect: str = "") -> bytes:
    return package({
        "_rels/.rels": rels(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": f"<w:document {DECL}><w:body>{body}<w:sectPr>{sect}</w:sectPr></w:body></w:document>",
        "word/_rels/document.xml.rels": rels(*document_rels),
        **(parts or {}),
    })


def drawing(inner: str) -> str:
    return f"<w:r><w:drawing><wp:inline><a:graphic><a:graphicData>{inner}</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"


CHART = (f"<c:chartSpace {DECL}><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Grade weights</a:t></a:r></a:p>"
         "</c:rich></c:tx></c:title><c:plotArea><c:pieChart><c:ser><c:tx><c:strRef><c:strCache><c:pt idx=\"0\">"
         "<c:v>Weight</c:v></c:pt></c:strCache></c:strRef></c:tx><c:cat><c:strRef><c:strCache>"
         "<c:pt idx=\"0\"><c:v>Exams</c:v></c:pt><c:pt idx=\"1\"><c:v>Homework</c:v></c:pt></c:strCache></c:strRef>"
         "</c:cat><c:val><c:numRef><c:numCache><c:pt idx=\"0\"><c:v>60</c:v></c:pt><c:pt idx=\"1\"><c:v>40</c:v>"
         "</c:pt></c:numCache></c:numRef></c:val></c:ser></c:pieChart></c:plotArea></c:chart></c:chartSpace>")
DIAGRAM = (f"<dgm:dataModel {DECL}><dgm:ptLst><dgm:pt modelId=\"1\" type=\"doc\"><dgm:t/></dgm:pt>"
           "<dgm:pt modelId=\"2\"><dgm:t><a:p><a:r><a:t>Plan</a:t></a:r></a:p></dgm:t></dgm:pt>"
           "<dgm:pt modelId=\"3\"><dgm:t><a:p><a:r><a:t>Draft</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>")

TEXT_BOX = (
    "<w:p><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wp:anchor><a:graphic><a:graphicData>"
    "<wps:wsp><wps:txbx><w:txbxContent>" + para("TEXTBOX-ONCE") + "</w:txbxContent></wps:txbx></wps:wsp>"
    "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>"
    "<w:txbxContent>" + para("TEXTBOX-ONCE") + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
    "</mc:AlternateContent></w:r></w:p>"
)

FULL_BODY = "".join([
    para("Body start."),
    "<w:sdt><w:sdtPr><w:alias w:val=\"Exam\"/></w:sdtPr><w:sdtContent>" + para("BLOCK-CONTROL final exam Dec 12")
    + "</w:sdtContent></w:sdt>",
    "<w:p>" + run("Inline: ") + "<w:sdt><w:sdtPr/><w:sdtContent>" + run("INLINE-CONTROL") + "</w:sdtContent></w:sdt></w:p>",
    "<w:tbl><w:tblPr/><w:tblGrid/>",
    "<w:tr><w:tc><w:tcPr/>" + para("Week") + "</w:tc><w:sdt><w:sdtContent><w:tc>" + para("CELL-CONTROL")
    + "</w:tc></w:sdtContent></w:sdt></w:tr>",
    "<w:sdt><w:sdtContent><w:tr><w:tc>" + para("ROW-CONTROL") + "</w:tc><w:tc>" + para("Quiz")
    + "<w:tbl><w:tr><w:tc>" + para("NESTED-TABLE") + "</w:tc></w:tr></w:tbl></w:tc></w:tr></w:sdtContent></w:sdt>",
    "</w:tbl>",
    TEXT_BOX,
    "<w:p><w:hyperlink r:id=\"rIdLink\">" + run("Course site") + "</w:hyperlink></w:p>",
    "<w:p><w:ins>" + run("INSERTED-TEXT") + "</w:ins><w:del><w:r><w:delText>DELETED-TEXT</w:delText></w:r></w:del></w:p>",
    "<w:p><w:r><w:fldChar w:fldCharType=\"begin\"/></w:r><w:r><w:instrText>DATE-CODE</w:instrText></w:r>"
    "<w:r><w:fldChar w:fldCharType=\"separate\"/></w:r>" + run("FIELD-RESULT")
    + "<w:r><w:fldChar w:fldCharType=\"end\"/></w:r></w:p>",
    "<w:p><w:fldSimple w:instr=\"PAGE\">" + run("SIMPLE-FIELD") + "</w:fldSimple></w:p>",
    "<w:p><m:oMathPara><m:oMath><m:r><m:t>x=2</m:t></m:r></m:oMath></m:oMathPara></w:p>",
    "<w:p><w:r><w:sym w:font=\"Wingdings\" w:char=\"F0FC\"/><w:t xml:space=\"preserve\"> done</w:t></w:r></w:p>",
    "<w:p>" + run("See note") + "<w:r><w:footnoteReference w:id=\"2\"/></w:r></w:p>",
    "<w:p>" + drawing("<pic:pic/>") + "</w:p>",
    "<w:p>" + drawing("<c:chart r:id=\"rIdChart\"/>") + "</w:p>",
    "<w:p>" + drawing("<dgm:relIds r:dm=\"rIdDm\"/>") + "</w:p>",
    "<w:altChunk r:id=\"rIdChunk\"/>",
    "<w:customXml w:element=\"x\">" + para("CUSTOM-XML") + "</w:customXml>",
    "<w:p><w:r><w:pict><v:shape><v:textpath string=\"WORDART-TEXT\"/></v:shape></w:pict></w:r></w:p>",
])
FULL_RELS = (
    ("rIdLink", "hyperlink", "https://canvas.example.edu/courses/1", True),
    ("rIdH", "header", "header1.xml"), ("rIdF", "footer", "footer1.xml"),
    ("rIdFn", "footnotes", "footnotes.xml"), ("rIdEn", "endnotes", "endnotes.xml"),
    ("rIdCm", "comments", "comments.xml"), ("rIdChart", "chart", "charts/chart1.xml"),
    ("rIdDm", "diagramData", "diagrams/data1.xml"), ("rIdChunk", "aFChunk", "afchunk.htm"),
)
FULL_PARTS = {
    "word/header1.xml": f"<w:hdr {DECL}>{para('HEADER-TEXT CS 101')}</w:hdr>",
    "word/footer1.xml": f"<w:ftr {DECL}>{para('FOOTER-TEXT')}</w:ftr>",
    "word/footnotes.xml": (f"<w:footnotes {DECL}><w:footnote w:type=\"separator\" w:id=\"-1\"><w:p><w:r><w:separator/>"
                           "</w:r></w:p></w:footnote><w:footnote w:id=\"2\"><w:p><w:r><w:footnoteRef/></w:r>"
                           + run("FOOTNOTE-TEXT") + "</w:p></w:footnote></w:footnotes>"),
    "word/endnotes.xml": f"<w:endnotes {DECL}><w:endnote w:id=\"1\">{para('ENDNOTE-TEXT')}</w:endnote></w:endnotes>",
    "word/comments.xml": (f"<w:comments {DECL}><w:comment w:id=\"0\" w:author=\"Prof\">{para('COMMENT-TEXT')}"
                          "</w:comment></w:comments>"),
    "word/charts/chart1.xml": CHART,
    "word/diagrams/data1.xml": DIAGRAM,
    "word/afchunk.htm": "<html><body><p>ALTCHUNK-HTML</p><script>no()</script></body></html>",
}
FULL_SECT = '<w:headerReference w:type="default" r:id="rIdH"/><w:footerReference w:type="default" r:id="rIdF"/>'


def text_of(result) -> str:
    return "\n\n".join(result.blocks)


class WordTests(unittest.TestCase):
    def setUp(self):
        self.result = extract_document(docx(FULL_BODY, FULL_RELS, FULL_PARTS, FULL_SECT), "docx")
        self.text = text_of(self.result)

    def test_every_text_container_is_read_exactly_once(self):
        for marker in (
            "Body start.", "BLOCK-CONTROL final exam Dec 12", "INLINE-CONTROL", "CELL-CONTROL", "ROW-CONTROL",
            "NESTED-TABLE", "TEXTBOX-ONCE", "INSERTED-TEXT", "FIELD-RESULT", "SIMPLE-FIELD", "x=2",
            "HEADER-TEXT CS 101", "FOOTER-TEXT", "FOOTNOTE-TEXT", "ENDNOTE-TEXT", "COMMENT-TEXT",
            "ALTCHUNK-HTML", "CUSTOM-XML", "WORDART-TEXT", "[Diagram] Plan | Draft", "[Chart] Grade weights",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(self.text.count(marker), 1, self.text)

    def test_deleted_text_field_codes_and_scripts_are_not_content(self):
        for absent in ("DELETED-TEXT", "DATE-CODE", "no()"):
            self.assertNotIn(absent, self.text)

    def test_structure_is_kept_readable(self):
        self.assertTrue(self.text.startswith("Body start."))
        self.assertIn("[Header]\nHEADER-TEXT CS 101", self.text)
        self.assertIn("Inline: INLINE-CONTROL", self.text)
        self.assertIn("Week\tCELL-CONTROL", self.text)
        self.assertIn("ROW-CONTROL\tQuiz\nNESTED-TABLE", self.text)
        self.assertIn("Course site <https://canvas.example.edu/courses/1>", self.text)
        self.assertIn("• done", self.text)
        self.assertIn("See note[footnote 2]", self.text)
        self.assertIn("[Footnotes]\n[footnote 2] FOOTNOTE-TEXT", self.text)
        self.assertIn("[Endnotes]\n[endnote 1] ENDNOTE-TEXT", self.text)
        self.assertIn("[Comments]\n- COMMENT-TEXT", self.text)
        self.assertIn("Weight: Exams = 60; Homework = 40", self.text)
        self.assertLess(self.text.index("[Header]"), self.text.index("[Footer]"))

    def test_pictures_are_reported_but_do_not_hide_text(self):
        self.assertTrue(self.result.complete)
        self.assertTrue(any("1 image" in warning for warning in self.result.warnings), self.result.warnings)

    def test_embedded_objects_and_unreadable_chunks_mark_coverage_incomplete(self):
        body = para("Intro") + "<w:p><w:r><w:object><v:shape><v:imagedata r:id=\"x\"/></v:shape></w:object></w:r></w:p>"
        result = extract_document(docx(body), "docx")
        self.assertFalse(result.complete)
        self.assertTrue(any("embedded object" in warning for warning in result.warnings))
        self.assertFalse(any("image" in warning for warning in result.warnings))
        chunk = docx(para("Intro") + '<w:altChunk r:id="rIdC"/>', [("rIdC", "aFChunk", "chunk.bin")],
                     {"word/chunk.bin": b"\x00\x01"})
        result = extract_document(chunk, "docx")
        self.assertFalse(result.complete)
        self.assertTrue(any("chunk.bin" in error for error in result.errors))

    def test_strict_open_xml_namespaces(self):
        archive = zipfile.ZipFile(io.BytesIO(docx(FULL_BODY, FULL_RELS, FULL_PARTS, FULL_SECT)))
        files = {name: archive.read(name).replace(NS["w"].encode(), b"http://purl.oclc.org/ooxml/wordprocessingml/main")
                 for name in archive.namelist() if name != "[Content_Types].xml"}
        text = text_of(extract_document(package(files), "docx"))
        self.assertIn("BLOCK-CONTROL final exam Dec 12", text)
        self.assertIn("HEADER-TEXT CS 101", text)

    def test_nested_word_chunk_and_expansion_budget(self):
        inner = docx(para("NESTED-DOCX"))
        outer = docx(para("Outer") + '<w:altChunk r:id="rIdC"/>', [("rIdC", "aFChunk", "inner.docx")],
                     {"word/inner.docx": inner})
        self.assertIn("NESTED-DOCX", text_of(extract_document(outer, "docx")))
        with patch.object(extract, "MAX_EXPANDED_BYTES", 10):
            with self.assertRaises(DocumentError) as raised:
                extract_document(outer, "docx")
        self.assertEqual(raised.exception.status, "too_large")

    def test_embedded_word_chunks_share_the_expansion_budget(self):
        # Each nested document fits; their combined decompression must not get
        # a fresh allowance for every embedding (or repeated reference).
        inner = docx(para("N" * 40_000))
        one = docx(para("Outer") + '<w:altChunk r:id="rIdC"/>',
                   [("rIdC", "aFChunk", "inner.docx")], {"word/inner.docx": inner})
        repeated = docx(para("Outer") + '<w:altChunk r:id="rIdC"/>' * 2,
                        [("rIdC", "aFChunk", "inner.docx")], {"word/inner.docx": inner})
        with patch.object(extract, "MAX_EXPANDED_BYTES", 80_000):
            self.assertIn("N" * 40_000, text_of(extract_document(one, "docx")))
            with self.assertRaises(DocumentError) as raised:
                extract_document(repeated, "docx")
        self.assertEqual(raised.exception.status, "too_large")

    def test_missing_referenced_word_parts_mark_partial(self):
        cases = (
            ("header", "header1.xml", '<w:headerReference w:type="default" r:id="rIdX"/>', ""),
            ("footer", "footer1.xml", '<w:footerReference w:type="default" r:id="rIdX"/>', ""),
            ("footnotes", "footnotes.xml", "", '<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'),
            ("comments", "comments.xml", "", '<w:p><w:r><w:commentReference w:id="1"/></w:r></w:p>'),
        )
        for kind, name, sect, body in cases:
            with self.subTest(kind=kind):
                data = docx(para("BODY-SURVIVES") + body, [("rIdX", kind, name)], sect=sect)
                result = extract_document(data, "docx")
                self.assertIn("BODY-SURVIVES", text_of(result))
                self.assertFalse(result.complete)
                self.assertTrue(any(name in error for error in result.errors), result.errors)

    def test_missing_reference_relationship_marks_partial(self):
        data = docx(para("BODY-SURVIVES"), sect='<w:headerReference w:type="default" r:id="missing"/>')
        result = extract_document(data, "docx")
        self.assertFalse(result.complete)
        self.assertTrue(any("header" in error and "missing relationship" in error for error in result.errors))

    def test_missing_chart_and_smartart_parts_mark_partial(self):
        body = para("BODY-SURVIVES") + "<w:p>" + drawing('<c:chart r:id="chart"/>') + "</w:p>"
        body += "<w:p>" + drawing('<dgm:relIds r:dm="diagram"/>') + "</w:p>"
        result = extract_document(docx(body, [("chart", "chart", "charts/missing.xml"),
                                              ("diagram", "diagramData", "diagrams/missing.xml")]), "docx")
        self.assertIn("BODY-SURVIVES", text_of(result))
        self.assertFalse(result.complete)
        self.assertTrue(any("chart" in error and "SmartArt" in error for error in result.errors))

    def test_damaged_part_is_repaired_and_reported(self):
        header = f'<w:hdr xmlns:w="{NS["w"]}"><w:p><w:r><w:t>HEADER-SURVIVES</w:t></w:r></w:p>'  # no closing tag
        data = docx(para("BODY-SURVIVES"), [("rIdH", "header", "header1.xml")], {"word/header1.xml": header},
                    '<w:headerReference w:type="default" r:id="rIdH"/>')
        result = extract_document(data, "docx")
        self.assertIn("BODY-SURVIVES", text_of(result))
        self.assertFalse(result.complete)
        self.assertTrue(any("header1.xml" in error for error in result.errors), result.errors)

    def test_failing_chart_keeps_the_rest_of_the_document(self):
        with patch.object(extract, "_chart_lines", side_effect=RuntimeError("boom")):
            result = extract_document(docx(FULL_BODY, FULL_RELS, FULL_PARTS, FULL_SECT), "docx")
        text = text_of(result)
        self.assertIn("BLOCK-CONTROL final exam Dec 12", text)
        self.assertIn("HEADER-TEXT CS 101", text)
        self.assertFalse(result.complete)
        self.assertTrue(any(error.startswith("Could not read") for error in result.errors), result.errors)

    def test_damaged_file_is_unreadable(self):
        with self.assertRaises(DocumentError) as raised:
            extract_document(b"PK\x03\x04 not really a zip", "docx")
        self.assertEqual(raised.exception.status, "unreadable")


def shape(text: str, placeholder: str | None = None) -> str:
    ph = f'<p:ph type="{placeholder}"/>' if placeholder else ""
    paragraphs = "".join(f"<a:p><a:r><a:rPr lang=\"en-US\"/><a:t>{line}</a:t></a:r></a:p>" for line in text.split("|"))
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="s"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:spPr/>'
            f"<p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>")


def frame(inner: str) -> str:
    return f"<p:graphicFrame><p:nvGraphicFramePr/><a:graphic><a:graphicData>{inner}</a:graphicData></a:graphic></p:graphicFrame>"


def pptx(slides, slide_rels=None, parts=None, hidden=()) -> bytes:
    files = {
        "_rels/.rels": rels(("rId1", "officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": (f"<p:presentation {DECL}><p:sldIdLst>"
                                 + "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
                                 + "</p:sldIdLst></p:presentation>"),
        "ppt/_rels/presentation.xml.rels": rels(*[(f"rId{i + 1}", "slide", f"slides/slide{i + 1}.xml")
                                                  for i in range(len(slides))]),
    }
    for number, tree in enumerate(slides, 1):
        show = ' show="0"' if number in hidden else ""
        files[f"ppt/slides/slide{number}.xml"] = (f"<p:sld {DECL}{show}><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>"
                                                  f"{tree}</p:spTree></p:cSld></p:sld>")
        files[f"ppt/slides/_rels/slide{number}.xml.rels"] = rels(*(slide_rels or {}).get(number, ()))
    files.update(parts or {})
    return package(files)


TABLE = ("<a:tbl><a:tr><a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>Week</a:t></a:r></a:p></a:txBody></a:tc>"
         "<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>Topic</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>")
SLIDE_ONE = "".join([
    shape("BODY-FIRST-IN-TREE|Second bullet"),
    shape("SLIDE-TITLE", "title"),
    "<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/>" + shape("GROUPED-TEXT") + "</p:grpSp>",
    frame(TABLE),
    "<p:pic/>",
    "<mc:AlternateContent><mc:Choice Requires=\"p14\">" + shape("ALT-ONCE") + "</mc:Choice><mc:Fallback>"
    + shape("ALT-ONCE") + "</mc:Fallback></mc:AlternateContent>",
    frame('<c:chart r:id="rIdChart"/>'),
    frame('<dgm:relIds r:dm="rIdDm"/>'),
])
NOTES = (f"<p:notes {DECL}><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>{shape('THUMBNAIL', 'sldImg')}"
         f"{shape('NOTES-TEXT', 'body')}{shape('7', 'sldNum')}</p:spTree></p:cSld></p:notes>")
LEGACY_COMMENTS = f"<p:cmLst {DECL}><p:cm authorId=\"0\"><p:pos x=\"1\" y=\"1\"/><p:text>LEGACY-COMMENT</p:text></p:cm></p:cmLst>"
MODERN_COMMENTS = (f"<p188:cmLst {DECL}><p188:cm id=\"1\"><p188:txBody><a:bodyPr/><a:p><a:r><a:t>MODERN-COMMENT</a:t>"
                   "</a:r></a:p></p188:txBody><p188:replyLst><p188:reply id=\"2\"><p188:txBody><a:bodyPr/><a:p><a:r>"
                   "<a:t>REPLY-TEXT</a:t></a:r></a:p></p188:txBody></p188:reply></p188:replyLst></p188:cm></p188:cmLst>")
MODERN = "http://schemas.microsoft.com/office/2018/10/relationships/comments"


class PowerPointTests(unittest.TestCase):
    def setUp(self):
        data = pptx(
            [SLIDE_ONE, shape("HIDDEN-SLIDE-TEXT"), "<p:pic/>"],
            slide_rels={
                1: [("rIdChart", "chart", "../charts/chart1.xml"), ("rIdDm", "diagramData", "../diagrams/data1.xml"),
                    ("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml"), ("rIdC", "comments", "../comments/comment1.xml")],
                2: [("rIdC", MODERN, "../comments/modernComment_1.xml")],
            },
            parts={"ppt/charts/chart1.xml": CHART, "ppt/diagrams/data1.xml": DIAGRAM,
                   "ppt/notesSlides/notesSlide1.xml": NOTES, "ppt/comments/comment1.xml": LEGACY_COMMENTS,
                   "ppt/comments/modernComment_1.xml": MODERN_COMMENTS},
            hidden={2},
        )
        self.result = extract_document(data, "pptx")
        self.text = text_of(self.result)

    def test_one_bad_slide_does_not_lose_the_others(self):
        data = pptx([shape("FIRST-SLIDE"), shape("SECOND-SLIDE")])
        original = extract._Package.xml
        def broken(package, name, *args, **kwargs):
            if name.endswith("slide2.xml"):
                raise RuntimeError("boom")
            return original(package, name, *args, **kwargs)
        with patch.object(extract._Package, "xml", broken):
            result = extract_document(data, "pptx")
        text = text_of(result)
        self.assertIn("FIRST-SLIDE", text)
        self.assertIn("(this slide could not be read)", text)
        self.assertTrue(result.errors)

    def test_failing_notes_keep_slide_text(self):
        with patch.object(extract, "_notes_lines", side_effect=RuntimeError("boom")):
            result = extract_document(pptx([SLIDE_ONE], slide_rels={1: [("rIdN", "notesSlide", "../notesSlides/notesSlide1.xml")]},
                                           parts={"ppt/notesSlides/notesSlide1.xml": NOTES}), "pptx")
        self.assertIn("SLIDE-TITLE", text_of(result))
        self.assertTrue(result.errors)

    def test_missing_referenced_notes_and_comments_mark_partial(self):
        for kind, target, expected in (("notesSlide", "../notesSlides/missing.xml", "notes"),
                                       ("comments", "../comments/missing.xml", "comments")):
            with self.subTest(kind=kind):
                data = pptx([shape("SLIDE-SURVIVES")], slide_rels={1: [("rIdX", kind, target)]})
                result = extract_document(data, "pptx")
                self.assertIn("SLIDE-SURVIVES", text_of(result))
                self.assertFalse(result.complete)
                self.assertTrue(any(expected in error for error in result.errors), result.errors)

    def test_slides_in_order_with_title_first(self):
        self.assertEqual(self.result.units, 3)
        self.assertTrue(self.text.startswith("[Slide 1]\nSLIDE-TITLE\nBODY-FIRST-IN-TREE\nSecond bullet"))
        self.assertIn("[Slide 2 (hidden)]\nHIDDEN-SLIDE-TEXT", self.text)
        self.assertIn("[Slide 3]", self.text)

    def test_every_slide_container_is_read_once(self):
        for marker in ("GROUPED-TEXT", "Week\tTopic", "ALT-ONCE", "[Chart] Grade weights",
                       "Weight: Exams = 60; Homework = 40", "[Diagram] Plan | Draft", "NOTES-TEXT",
                       "LEGACY-COMMENT", "MODERN-COMMENT / REPLY-TEXT"):
            with self.subTest(marker=marker):
                self.assertEqual(self.text.count(marker), 1, self.text)
        self.assertIn("[Notes]\nNOTES-TEXT", self.text)
        self.assertNotIn("THUMBNAIL", self.text)

    def test_pictures_and_hidden_slides_are_reported(self):
        self.assertTrue(self.result.complete)
        self.assertTrue(any("2 picture" in warning for warning in self.result.warnings), self.result.warnings)
        self.assertTrue(any("1 hidden slide" in warning for warning in self.result.warnings))

    def test_embedded_object_marks_incomplete(self):
        result = extract_document(pptx([shape("Title", "title") + frame('<p:oleObj r:id="x"/>')]), "pptx")
        self.assertFalse(result.complete)
        self.assertTrue(any("embedded object" in warning for warning in result.warnings))

    def test_picture_only_deck_is_reported_as_scanned(self):
        with self.assertRaises(DocumentError) as raised:
            extract_document(pptx(["<p:pic/>", "<p:pic/>"]), "pptx")
        self.assertEqual(raised.exception.status, "scanned_or_empty")


class FormatTests(unittest.TestCase):
    def test_kinds_from_extension_then_mime(self):
        cases = {
            ("slides.pptx", "application/octet-stream"): "pptx",
            ("lecture", "application/vnd.openxmlformats-officedocument.presentationml.presentation"): "pptx",
            ("syllabus.docx", "text/plain"): "docx",
            ("notes.RTF", "application/octet-stream"): "rtf",
            ("page", "text/html; charset=utf-8"): "html",
            ("data.csv", "text/csv"): "text",
        }
        for (name, mime), kind in cases.items():
            self.assertEqual(document_kind(name, mime), kind)
        for name, mime in (("old.doc", "application/msword"), ("deck.ppt", "application/vnd.ms-powerpoint"),
                           ("grades.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")):
            with self.assertRaises(DocumentError):
                document_kind(name, mime)

    def test_rtf_visible_text(self):
        raw = (r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}{\*\generator Word;}\f0 Hello \b bold\b0  caf\'e9 "
               "\\u8212? end\\par Next\\tab line}")
        self.assertEqual(rtf_text(raw), "Hello bold café — end\nNext\tline")
        self.assertEqual(text_of(extract_document(raw.encode(), "rtf")), "Hello bold café — end\nNext\tline")

    def test_rtf_utf16_pairs_and_invalid_units_are_valid_unicode(self):
        for raw, expected in (
            (r"{\rtf1\ansi caf\'e9 \u-10179?\u-8704?}", "café 😀"),
            (r"{\rtf1\ansi \uc0\u55357\u56832}", "😀"),
            (r"{\rtf1\ansi \u-10179? X \u-8704?}", "\ufffd X \ufffd"),
            (r"{\rtf1\ansi \u99999999?}", "\ufffd"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(rtf_text(raw), expected)
                self.assertEqual(text_of(extract_document(raw.encode(), "rtf")), expected)
                self.assertEqual(expected.encode("utf-8").decode("utf-8"), expected)


if __name__ == "__main__":
    unittest.main()
