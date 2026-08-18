"""Unit tests for the saved-search extractor's paging/mapping (mocked SOAP client).

saved_search has no live round-trip coverage: no RESTlet/SOAP sandbox fixture was available, so the
SOAP request shape is validated offline against the bundled WSDL and the paging/mapping logic is
exercised here with lightweight fake result objects. These tests lock the searchMoreWithId paging
loop and record mapping; they do not prove behaviour against a live NetSuite endpoint."""

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


def test_search_record_type_forwarded():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}])
    ext = SavedSearchExtractor(row=_row(load_type="full_load", search_record_type="Customer"), soap_client=client)
    ext.extract()
    _, kwargs = client.run_saved_search.call_args
    assert kwargs["search_record_type"] == "Customer"


def test_native_column_types_inferred_from_rows():
    # T2: saved_search output must carry native column types inferred from the mapped records.
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1", "amount": 9.0, "count": 2, "flag": False}])
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    table = ext.extract().tables[0]
    assert table.columns == ["id", "amount", "count", "flag"]
    assert table.column_types == {"id": "string", "amount": "numeric", "count": "integer", "flag": "boolean"}


def test_incremental_flag_reflected_on_table():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}])
    ext = SavedSearchExtractor(row=_row(load_type="incremental_load", primary_key=["id"]), soap_client=client)
    result = ext.extract()
    assert result.tables[0].incremental is True


def test_no_state_produced():
    client = mock.Mock()
    client.run_saved_search.return_value = _result([{"id": "1"}])
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    result = ext.extract()
    assert not hasattr(result, "state")
