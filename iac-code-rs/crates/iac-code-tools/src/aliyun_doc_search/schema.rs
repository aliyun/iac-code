use iac_code_protocol::json::{self, JsonValue};

pub(super) fn input_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([
                (
                    "keywords",
                    json::object([
                        ("type", json::string("string")),
                        ("description", json::string("Search keywords")),
                    ]),
                ),
                (
                    "category_id",
                    json::object([
                        ("type", json::string("integer")),
                        (
                            "description",
                            json::string(
                                "Product category ID, e.g. 28850 for ROS. Omit to search all products.",
                            ),
                        ),
                    ]),
                ),
            ]),
        ),
        ("required", json::array([json::string("keywords")])),
    ])
}
