use iac_code_protocol::json::{self, JsonValue};

pub(crate) const SUPPORTED_ACTIONS: &[&str] = &[
    "CreateStackInstances",
    "UpdateStackInstances",
    "DeleteStackInstances",
];

pub(crate) fn input_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([
                (
                    "action",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "enum",
                            json::array(SUPPORTED_ACTIONS.iter().copied().map(json::string)),
                        ),
                        (
                            "description",
                            json::string("The stack instances lifecycle action to perform."),
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
                            json::string("The region to perform the action in."),
                        ),
                    ]),
                ),
            ]),
        ),
        ("required", json::array([json::string("action")])),
    ])
}
