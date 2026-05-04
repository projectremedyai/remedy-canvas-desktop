from __future__ import annotations

from zipfile import ZIP_STORED, ZipFile

from crd_sidecar.crd_core.models import ContentType
from crd_sidecar.imscc.parser import parse


def test_parse_minimal_imscc_wiki_page(tmp_path):
    archive_path = tmp_path / "course.imscc"
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1"
          xmlns:lomimscc="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/manifest"
          identifier="manifest">
  <metadata>
    <schemaversion>1.1.0</schemaversion>
    <lomimscc:lom>
      <lomimscc:general>
        <lomimscc:title>
          <lomimscc:string>Smoke Course</lomimscc:string>
        </lomimscc:title>
      </lomimscc:general>
    </lomimscc:lom>
  </metadata>
  <resources>
    <resource identifier="page-1" type="webcontent" href="wiki_content/page.html">
      <file href="wiki_content/page.html"/>
    </resource>
  </resources>
</manifest>
"""
    page_html = "<html><head><title>Intro</title></head><body><p>Hello</p></body></html>"

    with ZipFile(archive_path, "w", compression=ZIP_STORED) as zf:
        zf.writestr("imsmanifest.xml", manifest)
        zf.writestr("wiki_content/page.html", page_html)

    parsed = parse(archive_path)

    assert parsed.course_title == "Smoke Course"
    assert parsed.schema_version == "1.1.0"
    assert len(parsed.pages) == 1
    assert parsed.pages[0].title == "Intro"
    assert parsed.pages[0].content_type == ContentType.WIKI_PAGE
    assert parsed.pages[0].file_path == "wiki_content/page.html"
