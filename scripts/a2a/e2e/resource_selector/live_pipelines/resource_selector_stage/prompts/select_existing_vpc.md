This is a live, read-only A2A resource-selector test step.

You must let the user select exactly one existing VPC in the region stated by the user (cn-hangzhou by default).
Resolve the selector with the short English query `VPC`, then call `select_cloud_resource` with the resolved
`vpc.vpc` contract. Do not list resources yourself and do not call another cloud API. After the structured
selection result is returned, respect it and call `complete_step` with `{"conclusion":{"status":"selected"}}`.
Do not finish the step before the selection result is returned.
