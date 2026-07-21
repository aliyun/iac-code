# Aliyun 元数据维护脚本

## 用途

本目录集中维护 Aliyun API 运行时数据的生成器：

- `generate_endpoints.py`：生成 `data/endpoints/` 下的 Endpoint 目录、不可用清单和报告；
- `generate_oss_operations.py`：生成 `data/oss/operation_catalog.json`；
- `generate_product_catalog.py`：生成 `data/openmeta/product_catalog.json`；
- `generate_product_matching_fixture.py`：生成 Product 匹配测试 fixture。

运行时数据的目录职责和人工维护边界见 `src/iac_code/tools/cloud/aliyun/data/README.md`。

## Product 离线目录

`src/iac_code/tools/cloud/aliyun/data/openmeta/product_catalog.json` 是运行时 Product Code、shortName 和版本摘要的识别依据。应用运行时不会下载 OpenMeta 全量产品列表；阿里云新增或修改产品后，需要在开发阶段重新生成该文件并随版本发布。

生成过程只访问公开元数据，不读取 AccessKey、STS Token 或 `~/.iac-code/.cloud-credentials.yml`。

## 更新步骤

在仓库根目录执行：

```bash
curl --fail --silent --show-error --location \
  'https://api.aliyun.com/meta/v1/products.json?language=ZH_CN' \
  --output /tmp/aliyun-products.json

uv run python scripts/aliyun/generate_product_catalog.py \
  --input /tmp/aliyun-products.json \
  --output src/iac_code/tools/cloud/aliyun/data/openmeta/product_catalog.json
```

`/tmp/aliyun-products.json` 就是官方接口直接返回的 JSON，无需转换成其他格式。官方响应当前是一个产品对象数组。

生成脚本会：

1. 校验 Product Code、安全的不透明版本标识和产品字段；保留 `2024-06-11`、`20240611`、`iap_1.0` 等官方格式。
2. 只保留 `code`、`shortName`、`defaultVersion`、`recommendVersions`、`versions` 和 `style`。
3. 清理 shortName 首尾 ASCII 空白。
4. 把 `暂无`、纯空白、序列化数组等不安全 shortName 写为 `null`。
5. 记录来源 URL、生成时间、源文件 SHA-256 和目录内容 SHA-256。
6. 按 Product Code 排序，生成可审查的稳定 JSON。

## Envelope 是什么

脚本也兼容项目 OpenMeta 缓存使用的 envelope：

```json
{
  "source_url": "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN",
  "fetched_at": "2026-07-18T06:50:47.107773+00:00",
  "payload_sha256": "...",
  "payload": {
    "products": []
  }
}
```

这是内部缓存格式，不是更新目录的前置要求。日常更新直接使用上一节的官方原始 JSON 即可。

## 审查与验证

生成后先查看目录差异，重点审查新增/删除 Product、默认版本变化、shortName 变化和异常版本：

```bash
git diff -- src/iac_code/tools/cloud/aliyun/data/openmeta/product_catalog.json
```

然后运行：

```bash
uv run --all-extras pytest \
  tests/tools/cloud/aliyun/test_product_resolver.py \
  tests/tools/cloud/aliyun/test_api_contract.py -q

make lint
git diff --check
```

若只是目录更新时间或源文件 SHA 变化，也必须确认产品内容差异符合预期后再提交。不要手工修改目录摘要；需要修正官方脏 shortName 时，应调整生成规则或加入经过审查的 `data/openmeta/product_aliases.yml` 配置，然后重新生成并测试。
