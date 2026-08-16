from sitesense.main import app


def test_prd_route_contract() -> None:
    schema = app.openapi()
    actual = {(method.upper(), path) for path, operations in schema["paths"].items() for method in operations}
    expected = {
        ("POST", "/api/projects/{project_id}/analyze"),
        ("GET", "/api/projects/{project_id}/analysis"),
        ("GET", "/api/projects/{project_id}/analysis/status"),
        ("GET", "/api/projects/{project_id}/layers"),
        ("GET", "/api/projects/{project_id}/metrics"),
        ("POST", "/api/projects/{project_id}/goals"),
        ("GET", "/api/projects/{project_id}/features"),
        ("GET", "/api/projects/{project_id}/opportunities"),
        ("GET", "/api/projects/{project_id}/scope"),
        ("GET", "/api/projects/{project_id}/proposal"),
        ("GET", "/api/projects/{project_id}/parcel"),
        ("GET", "/api/parcel-search"),
        ("POST", "/api/parcel-search"),
        ("POST", "/api/parcel/confirm"),
        ("GET", "/api/pricebook"),
        ("POST", "/api/pricebook"),
        ("PATCH", "/api/pricebook/items/{item_id}"),
        ("PATCH", "/api/features/{feature_id}"),
        ("DELETE", "/api/features/{feature_id}"),
        ("PATCH", "/api/opportunities/{opportunity_id}"),
        ("PATCH", "/api/scope/{scope_id}"),
        ("PATCH", "/api/proposals/{proposal_id}"),
        ("POST", "/api/proposals/{proposal_id}/publish"),
        ("POST", "/api/proposals/{proposal_id}/pdf"),
    }
    assert expected <= actual
