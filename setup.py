from setuptools import find_packages, setup

setup(
    name="transport-coordination",
    version="0.1.0",
    description="跨区域旅游包车联审与履约协同服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
