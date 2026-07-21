"""Unit tests for the saved-search extractor's paging/mapping/state (mocked SOAP client).

Full SOAP behaviour is VCR-covered later; here we lock the searchMoreWithId paging loop, record
mapping and watermark handling with lightweight fake result objects."""

from types import SimpleNamespace
from typing import Any
from unittest import mock

from configuration import SavedSearchRow
from extractor.saved_search import SavedSearchExtractor


def _result(records, total_pages=1, page_index=1, search_id="s1"):
    return SimpleNamespace(
        searchResult=SimpleNamespace(
            totalPages=total_pages,
            pageIndex=page_index,
            searchId=search_id,
            recordList=SimpleNamespace(record=records),
        )
    )


def _row(**kw) -> SavedSearchRow:
    params: dict[str, Any] = {
        "mode": "saved_search",
        "saved_search_id": "customsearch_x",
        "output_table_name": "ss",
        **kw,
    }
    return SavedSearchRow.model_validate(params)


def test_single_page_maps_records():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}, {"id": "2"}])
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    result = ext.extract()
    rows = list(result.tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2"]
    client.search_more_with_id.assert_not_called()


def test_search_more_with_id_paging():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}], total_pages=3, page_index=1)
    client.search_more_with_id.side_effect = [
        _result([{"id": "2"}], total_pages=3, page_index=2),
        _result([{"id": "3"}], total_pages=3, page_index=3),
    ]
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    result = ext.extract()
    rows = list(result.tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert client.search_more_with_id.call_count == 2


def test_state_captured_from_server_time():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}])
    ext = SavedSearchExtractor(
        row=_row(load_type="incremental_load"),
        soap_client=client,
        server_time_provider=lambda: "2024-05-01T00:00:00Z",
    )
    result = ext.extract()
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}
    assert result.tables[0].incremental is True
