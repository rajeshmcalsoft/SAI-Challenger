from setuptools import find_packages, setup

print(find_packages(''))
setup(
    name="sai-challenger",
    version='0.1',
    description='SAI Challenger core library',
    license='Apache 2.0',
    author='Andriy Kokhan',
    author_email='andriy.kokhan@gmail.com',
    url='https://github.com/opencomputeproject/SAI-Challenger',
    install_requires=[
        'ptf',
        'pyzmq',
    ],
    packages=[
        'saichallenger.common',
        'saichallenger.common.sai_client',
        'saichallenger.common.sai_client.sai_redis_client',
        'saichallenger.common.sai_client.sai_thrift_client',
        'saichallenger.common.sai_client.sai_zmq_client',
        'saichallenger.common.sai_dataplane',
        'saichallenger.common.sai_dataplane.utils',
        'saichallenger.common.sai_dataplane.ptf',
        'saichallenger.common.sai_dataplane.snappi',
    ],
    package_dir={
        'saichallenger.common': '',
    },
)
