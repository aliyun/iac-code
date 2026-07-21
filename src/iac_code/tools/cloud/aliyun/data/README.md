# Aliyun Runtime Data

This directory contains bundled runtime data. Python modules stay in the parent
package; generated catalogs and reviewed policy files are grouped by owner here.

- `endpoints/`: generated endpoint catalogs plus reviewed endpoint overrides.
- `openmeta/`: OpenMeta exclusions, API overrides, and offline product matching data.
- `validation/`: live-validation sampling and reviewed judgment records.
- `oss/`: the generated OSS SDK operation catalog.

Generated files must be updated through the scripts in `scripts/aliyun/`. Reviewed
YAML files are maintained directly and must include the provenance required by
their schema.
