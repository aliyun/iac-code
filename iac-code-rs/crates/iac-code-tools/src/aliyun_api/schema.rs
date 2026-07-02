use iac_code_protocol::json::{self, JsonValue};

use super::VERSION_MAP;

pub(super) fn input_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([
                (
                    "product",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string(
                                "The Aliyun product code (e.g. 'ros', 'ecs', 'rds', 'vpc').",
                            ),
                        ),
                    ]),
                ),
                (
                    "action",
                    json::object([
                        ("type", json::string("string")),
                        ("description", json::string("The API action to call.")),
                    ]),
                ),
                (
                    "version",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string(format!(
                                "API version. Optional for common products: {}.",
                                VERSION_MAP
                                    .iter()
                                    .map(|(product, version)| format!("{product}({version})"))
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            )),
                        ),
                    ]),
                ),
                (
                    "params",
                    json::object([
                        ("type", json::string("object")),
                        (
                            "description",
                            json::string("Parameters to pass to the action."),
                        ),
                    ]),
                ),
                (
                    "region_id",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string("The region to call the action in."),
                        ),
                    ]),
                ),
                (
                    "style",
                    json::object([
                        ("type", json::string("string")),
                        ("enum", json::array([json::string("RPC"), json::string("ROA")])),
                        (
                            "description",
                            json::string(
                                "API style. Defaults to 'RPC'. Use 'ROA' for RESTful APIs (e.g. CS, CR, FC).",
                            ),
                        ),
                    ]),
                ),
                (
                    "method",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "enum",
                            json::array([
                                json::string("GET"),
                                json::string("POST"),
                                json::string("PUT"),
                                json::string("DELETE"),
                            ]),
                        ),
                        (
                            "description",
                            json::string("HTTP method. Defaults to 'POST'. Only needed for ROA APIs."),
                        ),
                    ]),
                ),
                (
                    "pathname",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string("Request path. Defaults to '/'. Only needed for ROA APIs (e.g. '/clusters')."),
                        ),
                    ]),
                ),
                (
                    "body",
                    json::object([
                        ("type", json::string("object")),
                        (
                            "description",
                            json::string("Request body. Only needed for ROA POST/PUT APIs."),
                        ),
                    ]),
                ),
            ]),
        ),
        (
            "required",
            json::array([json::string("product"), json::string("action")]),
        ),
    ])
}
