from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository, normalize_resource_type


def test_normalize_resource_type_strips_ros_prefix():
    assert normalize_resource_type("ROS/ALIYUN::ECS::VPC") == "ALIYUN::ECS::VPC"
    assert normalize_resource_type("ALIYUN::ECS::VPC") == "ALIYUN::ECS::VPC"
    assert normalize_resource_type("Terraform/alicloud_vpc") is None


def test_default_repository_loads_vendored_raw_metas():
    repo = ArchitectureMetaRepository.load_default()

    ecs = repo.get_resource("ALIYUN::ECS::Instance")
    assert ecs is not None
    assert ecs.name_en == "ECS Instance"
    assert ecs.product_code == "ecs"
    assert ecs.category_code == "computing"

    related = ecs.related_properties_by_name["VSwitchId"]
    assert related.targets == ("ALIYUN::ECS::VSwitch",)


def test_repository_normalizes_main_resource_type():
    repo = ArchitectureMetaRepository.load_default()

    attachment = repo.get_resource("ALIYUN::VPC::EIPAssociation")

    assert attachment is not None
    assert attachment.main_resource_type is not None
    assert attachment.main_resource_type.resource_type == "ALIYUN::VPC::EIP"
    assert attachment.main_resource_type.ref_property == "AllocationId"


def test_repository_maps_product_to_category():
    repo = ArchitectureMetaRepository.load_default()

    product = repo.get_product("rds")

    assert product is not None
    assert product.name_en == "ApsaraDB RDS"
    assert repo.category_code_for_product("rds") == "database"
