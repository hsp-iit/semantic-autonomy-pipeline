from setuptools import find_packages, setup

package_name = 'semantic_autonomy'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(include=[package_name, package_name + '.*']),
    package_data={package_name: ['web_chat/templates/*.html']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Simone Micheletti',
    maintainer_email='simone_micheletti@outlook.it',
    description='Agent layers above semantic navigation and the manipulation approach',
    license='BSD 3-Clause License',
    entry_points={
        'console_scripts': [
            'approach_server = semantic_autonomy.approach_server:main',
            'mission_server = semantic_autonomy.mission_server:main',
            'robot_server = semantic_autonomy.robot_server:main',
            'camera_server = semantic_autonomy.camera_server:main',
            'chatter = semantic_autonomy.chatter:main',
            'chat_backend = semantic_autonomy.web_chat.backend:main',
        ],
        # Read by yarp_mcpServer_launcher: the entry name is the device type in the XML configuration
        'yarp_mcpServer_core.servers': [
            'Yarp_mcpServer_Robot = semantic_autonomy.robot_server:Yarp_mcpServer_Robot',
            'Yarp_mcpServer_Camera = semantic_autonomy.camera_server:Yarp_mcpServer_Camera',
            'Yarp_mcpServer_Approach = semantic_autonomy.approach_server:Yarp_mcpServer_Approach',
            'Yarp_mcpServer_Mission = semantic_autonomy.mission_server:Yarp_mcpServer_Mission',
        ],
    },
)
