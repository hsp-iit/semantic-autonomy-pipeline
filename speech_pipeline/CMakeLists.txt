cmake_minimum_required(VERSION 3.16)

project(speech_pipeline)

set(CMAKE_CXX_STANDARD 17)

################################## Necessary packages to compile ###################################
find_package(YARP REQUIRED)                                                                
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(ament_cmake REQUIRED)
find_package(ros2_vlmaps_interfaces REQUIRED)

#################################### Executables to be compiled ####################################
add_executable(speech_bridge src/speech_bridge.cpp)
ament_target_dependencies(speech_bridge
    "rclcpp"
    "std_msgs"
    "ros2_vlmaps_interfaces"
)

target_link_libraries(speech_bridge
    ${YARP_LIBRARIES}
)

install(TARGETS speech_bridge
    DESTINATION lib/${PROJECT_NAME}
)

ament_package()

install(TARGETS speech_bridge DESTINATION bin)