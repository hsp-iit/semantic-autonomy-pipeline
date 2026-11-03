# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
#
# Everything above navigation: semantic-autonomy-pipeline, visual-language-navigation, manipulation-approach and their
# dependencies (YARP with Python bindings, yarp-devices-ros2, yarp-mcpServers, yarp-mcpHosts, Grounding DINO,
# ultralytics, the Claude Code CLI). Nav2 and ergocub_navigation are not here: they run on the robot.
#
# Three repositories are private: pass a GitHub token as a build secret, so that it stays out of the image layers.
#   echo "$GITHUB_TOKEN" > /tmp/gh_token
#   DOCKER_BUILDKIT=1 docker build --secret id=github_token,src=/tmp/gh_token -t semantic_autonomy -f docker/Dockerfile .
# Rebuild the repositories only, keeping the heavy layers: add --build-arg REPOS_REV=$(date +%s)
# See docker/run.sh to start it.

FROM nvidia/cuda:12.8.0-devel-ubuntu24.04

ENV USER=user1
ARG PASSWORD=user1
# GPU architectures for the CUDA extension of Grounding DINO
ARG TORCH_CUDA_ARCH_LIST="7.5;8.6;8.9"
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

RUN ln -fs /usr/share/zoneinfo/Europe/Rome /etc/localtime && \
    apt-get update && \
    apt-get install --no-install-recommends -y sudo git cmake curl wget unzip ca-certificates gnupg locales \
        software-properties-common build-essential python3-pip ffmpeg libsm6 libxext6 \
        iproute2 bash-completion nano && \
    locale-gen en_US en_US.UTF-8 && update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8

# The paths in the configurations of the three repositories expect /home/user1. Same uid as the host user, so that
# mounted files (the Claude Code login, the maps) stay writable; the base image already has an ubuntu user on 1000.
ARG USER_UID=1000
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && groupadd -g ${USER_UID} ${USER} \
    && useradd -ms /bin/bash -u ${USER_UID} -g ${USER} ${USER} \
    && echo "${USER}:${PASSWORD}" | chpasswd \
    && usermod -a -G sudo ${USER} \
    && sed -i.bak -e 's/%sudo\s\+ALL=(ALL\(:ALL\)\?)\s\+ALL/%sudo ALL=NOPASSWD:ALL/g' /etc/sudoers
USER ${USER}
WORKDIR /home/${USER}

# ---------------------------------------------------------------- ROS 2 Jazzy
ENV ROS_DISTRO=jazzy
RUN sudo add-apt-repository -y universe && \
    export ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F\" '{print $4}') && \
    curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo $VERSION_CODENAME)_all.deb" && \
    sudo apt-get install -y /tmp/ros2-apt-source.deb && rm /tmp/ros2-apt-source.deb && \
    sudo apt-get update && sudo apt-get install --no-install-recommends -y \
        ros-$ROS_DISTRO-ros-base ros-dev-tools ros-$ROS_DISTRO-rmw-cyclonedds-cpp \
        ros-$ROS_DISTRO-rviz2 ros-$ROS_DISTRO-nav2-rviz-plugins \
        ros-$ROS_DISTRO-nav2-msgs ros-$ROS_DISTRO-nav2-simple-commander ros-$ROS_DISTRO-nav2-costmap-2d \
        ros-$ROS_DISTRO-pcl-ros ros-$ROS_DISTRO-tf2-ros ros-$ROS_DISTRO-tf2-geometry-msgs \
        ros-$ROS_DISTRO-tf2-sensor-msgs ros-$ROS_DISTRO-tf2-eigen ros-$ROS_DISTRO-message-filters \
        ros-$ROS_DISTRO-action-msgs ros-$ROS_DISTRO-actionlib-msgs ros-$ROS_DISTRO-test-msgs \
        ros-$ROS_DISTRO-rosbag2-py && \
    sudo rm -rf /var/lib/apt/lists/*
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

# ---------------------------------------------------------------- YARP 3.12, with Python bindings
RUN sudo apt-get update && sudo apt-get install --no-install-recommends -y \
        libeigen3-dev libace-dev libedit-dev libsqlite3-dev libtinyxml-dev libjpeg-dev libgraphviz-dev \
        qtbase5-dev qtdeclarative5-dev qtmultimedia5-dev libqcustomplot-dev \
        qml-module-qtquick2 qml-module-qtquick-window2 qml-module-qtmultimedia qml-module-qtquick-dialogs \
        qml-module-qtquick-controls qml-module-qt-labs-folderlistmodel qml-module-qt-labs-settings \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
        glpk-utils libglpk-dev python3-dev && \
    sudo rm -rf /var/lib/apt/lists/*
# SWIG 4.2.0 of Ubuntu 24.04 breaks the YARP bindings (robotology/yarp#3083): use YARP's SWIG 4.3.0 build (amd64)
RUN cd /tmp && wget -nv https://github.com/robotology/robotology-vcpkg-ports/releases/download/storage/swig_4_3_0_ubuntu_24_04.zip && \
    unzip -q swig_4_3_0_ubuntu_24_04.zip && \
    sudo mv swig_4_3_0_ubuntu_24_04_install/bin/* /usr/bin/ && \
    sudo mv swig_4_3_0_ubuntu_24_04_install/share/swig /usr/share/ && \
    rm -rf /tmp/swig_4_3_0_ubuntu_24_04* && swig -version | grep -q 4.3.0
RUN git clone https://github.com/robotology/ycm-cmake-modules && cd ycm-cmake-modules && mkdir build && cd build && \
    cmake .. && sudo make install -j$(nproc)
RUN git clone -b yarp-3.12 https://github.com/robotology/yarp && cd yarp && mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DYARP_COMPILE_BINDINGS=ON -DCREATE_PYTHON=ON \
          -DCMAKE_INSTALL_PYTHON3DIR=lib/python3.12/dist-packages -DSWIG_EXECUTABLE=/usr/bin/swig .. && \
    sudo make install -j$(nproc) && sudo ldconfig && \
    python3 -c "import yarp"
ENV YARP_COLORED_OUTPUT=1
ENV YARP_DATA_DIRS=/usr/local/share/yarp
# Camera and other bridges between YARP and ROS 2 (same version as on the development machine)
RUN git clone https://github.com/robotology/yarp-devices-ros2 && cd yarp-devices-ros2 && git checkout v2.0.0 && \
    cd ros2_interfaces_ws && source /opt/ros/$ROS_DISTRO/setup.bash && colcon build && \
    cd .. && mkdir build && cd build && \
    source /opt/ros/$ROS_DISTRO/setup.bash && source ../ros2_interfaces_ws/install/setup.bash && \
    cmake -DYARP_ROS2_USE_SYSTEM_map2d_nws_ros2_msgs=ON -DYARP_ROS2_USE_SYSTEM_yarp_control_msgs=ON .. && \
    make -j$(nproc)
ENV YARP_DATA_DIRS=${YARP_DATA_DIRS}:/home/${USER}/yarp-devices-ros2/build/share/yarp:/home/${USER}/yarp-devices-ros2/build/share/yarp-devices-ros2

# ---------------------------------------------------------------- Python: PyTorch and the vision models
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_NO_CACHE_DIR=1
ENV TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} FORCE_CUDA=1
ENV PATH=/home/${USER}/.local/bin:${PATH}
RUN pip3 install torch torchvision --index-url https://download.pytorch.org/whl/cu128
# visual-language-navigation (from its docker/Dockerfile_u24_cu128_yarp4, plus the imports it lacks), without the LSeg
# and FC-CLIP encoders: no detectron2, no openai-clip (only LSeg imports it), and only the RADIO checkpoint below
RUN pip3 install hydra-core gdown h5py timm pyvisgraph pytorch_lightning odfpy networkx \
        opencv-python matplotlib open3d "numpy>=2" scipy scikit-learn pandas psutil pynvml tqdm tabulate && \
    pip3 install --no-deps gtsam==4.3.0 && \
    pip3 install openai flask_socketio accelerate gradio rerun torch_scatter einops "transformers<5"
# manipulation-approach (from its docker/Dockerfile). Not ultralytics/CLIP: only text-prompted models use it, the
# detector prompts SAM 2 with boxes.
RUN pip3 install addict yapf "supervision>=0.22.0" pycocotools ultralytics shapely
RUN git clone -b cuda12.8-fix https://github.com/SimoneMic/GroundingDINO.git && \
    cd GroundingDINO && pip3 install --no-build-isolation -e . && \
    mkdir weights && wget -nv -O weights/groundingdino_swint_ogc.pth \
        https://github.com/IDEA-Research/GroundingDINO/releases/download/v0.1.0-alpha/groundingdino_swint_ogc.pth
# Grounding DINO downloads its text encoder on first use: cache it now
RUN python3 -c "from transformers import BertModel, BertTokenizer; BertTokenizer.from_pretrained('bert-base-uncased'); BertModel.from_pretrained('bert-base-uncased')"

# ---------------------------------------------------------------- Claude Code CLI, for the orchestrator and the navigator
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version

# ---------------------------------------------------------------- Repositories
ARG REPOS_REV=0
ARG SAP_BRANCH=main
ARG MA_BRANCH=simonemic/improvements
ARG VLN_BRANCH=feat/move_agents_out
ARG MCP_SERVERS_BRANCH=main
ARG MCP_HOSTS_BRANCH=main
# The token is used for the clone only: the remotes are reset to the public URL, so it is not saved in .git/config
RUN --mount=type=secret,id=github_token,required=true,mode=0444 \
    echo "repositories at ${REPOS_REV}" && \
    TOKEN=$(cat /run/secrets/github_token) && \
    clone() { git clone -b "$2" "https://x-access-token:${TOKEN}@github.com/hsp-iit/$1" "$3" && \
              git -C "$3" remote set-url origin "https://github.com/hsp-iit/$1"; } && \
    clone visual-language-navigation "${VLN_BRANCH}" visual-language-navigation && \
    clone yarp-mcpServers "${MCP_SERVERS_BRANCH}" yarp-mcpServers && \
    clone yarp-mcpHosts "${MCP_HOSTS_BRANCH}" yarp-mcpHosts && \
    mkdir -p ws_ros2/src && \
    clone semantic-autonomy-pipeline "${SAP_BRANCH}" ws_ros2/src/semantic-autonomy-pipeline && \
    clone manipulation-approach "${MA_BRANCH}" ws_ros2/src/manipulation-approach && \
    ! grep -rq "x-access-token" */.git/config ws_ros2/src/*/.git/config

# YARP MCP libraries: yarp_mcpServer_core first, then the others (mcp, uvicorn, httpx, openai come with them)
RUN cd yarp-mcpServers && python3 install_packages.py && \
    cd ../yarp-mcpHosts && python3 install_packages.py && \
    python3 -c "import yarp_mcpServer_core, yarp_mcpServer_ros2, yarp_mcpHost_core, yarp_mcpHost_generalPurpose"

# visual-language-navigation: the RADIO checkpoint, then the ROS packages. Not LSeg (download_hf_model.py and the CLIP
# ViT-B/32 weights) nor FC-CLIP: seg_model_name must stay radio (the default) or naradio
RUN cd visual-language-navigation/docker && python3 download_radio_ckp.py
RUN cd visual-language-navigation && source /opt/ros/$ROS_DISTRO/setup.bash && \
    colcon build --symlink-install --packages-select ros2_vlmaps_interfaces visual_language_navigation
# Without openai-clip, the map server only imports if voxel_feat_map.py imports clip_utils in the LSeg branch alone
RUN source /opt/ros/$ROS_DISTRO/setup.bash && source visual-language-navigation/install/local_setup.bash && \
    PYTHONPATH=~/visual-language-navigation:$PYTHONPATH python3 -c "import visual_language_navigation.map.semantic_map_server" || \
    (echo "The visual-language-navigation branch still needs openai-clip at import: push the voxel_feat_map.py change" && exit 1)

# manipulation-approach and semantic-autonomy-pipeline, in one workspace
RUN cd ws_ros2 && source /opt/ros/$ROS_DISTRO/setup.bash && \
    source ~/visual-language-navigation/install/local_setup.bash && \
    colcon build --symlink-install
# SAM 2 weights: the detector loads sam2.1_l.pt from its working directory, the repository root
RUN cd ws_ros2/src/manipulation-approach && \
    python3 -c "from ultralytics.utils.downloads import attempt_download_asset; attempt_download_asset('sam2.1_l.pt')" && \
    test -f sam2.1_l.pt

# ---------------------------------------------------------------- Environment
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> ~/.bashrc && \
    echo "source ~/yarp-devices-ros2/ros2_interfaces_ws/install/local_setup.bash" >> ~/.bashrc && \
    echo "source ~/visual-language-navigation/install/local_setup.bash" >> ~/.bashrc && \
    echo "source ~/ws_ros2/install/local_setup.bash" >> ~/.bashrc && \
    echo "export PYTHONPATH=~/ws_ros2/src/semantic-autonomy-pipeline/semantic_autonomy:~/visual-language-navigation:\$PYTHONPATH" >> ~/.bashrc && \
    echo "alias launch_vl_map_builder='ros2 launch visual_language_navigation build_map.launch.py sigterm_timeout:=120 sigkill_timeout:=130'" >> ~/.bashrc
