"""The Image Builder bake driver for per-version app AMIs: component (bake script)
-> recipe -> image, polled to AVAILABLE. Keeping the create->poll sequence in one
place stops bakes from drifting."""

from __future__ import annotations

import time
import uuid

from .. import config


def bake(ib, ssm, component_data: str, infra_arn: str, dist_arn: str) -> dict:
    """Returns the ami id plus the three Image Builder arns (the caller records them
    so the version can be torn down later). Raises RuntimeError on a failed build."""
    base_ami = ssm.get_parameter(Name=config.BASE_AMI_PARAM)["Parameter"]["Value"]
    name = f"openzi-{uuid.uuid4().hex[:16]}"

    component = ib.create_component(
        name=name, semanticVersion="1.0.0", platform="Linux", data=component_data)
    component_arn = component["componentBuildVersionArn"]
    recipe = ib.create_image_recipe(
        name=name, semanticVersion="1.0.0", parentImage=base_ami,
        components=[{"componentArn": component_arn}])
    recipe_arn = recipe["imageRecipeArn"]
    build = ib.create_image(
        imageRecipeArn=recipe_arn, infrastructureConfigurationArn=infra_arn,
        distributionConfigurationArn=dist_arn, imageTestsConfiguration={"imageTestsEnabled": False})
    image_arn = build["imageBuildVersionArn"]

    while True:
        img = ib.get_image(imageBuildVersionArn=image_arn)["image"]
        status = img["state"]["status"]
        if status == "AVAILABLE":
            return {"ami_id": img["outputResources"]["amis"][0]["image"],
                    "component_arn": component_arn, "recipe_arn": recipe_arn, "image_arn": image_arn}
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Image Builder build {status}")
        time.sleep(60)
