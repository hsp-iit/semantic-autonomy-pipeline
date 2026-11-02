#include <rclcpp/rclcpp.hpp>
#include <rclcpp/executor.hpp>
#include <std_msgs/msg/string.hpp>
#include "yarp/os/BufferedPort.h"
#include "yarp/os/Network.h"
#include "ros2_vlmaps_interfaces/srv/llm_chat.hpp"

class OnSpeech : public yarp::os::BufferedPort<yarp::os::Bottle>
{
private:
    // Service client
    rclcpp::Client<ros2_vlmaps_interfaces::srv::LlmChat>::SharedPtr m_chat_cli;
    // Executor for handling service client requests
    rclcpp::executors::SingleThreadedExecutor::SharedPtr m_exe;
public:
    // ROS2 public node interface
    rclcpp::Node::SharedPtr m_node;
    OnSpeech(std::string node_name);
    void onRead(yarp::os::Bottle& b) override;
};

OnSpeech::OnSpeech(std::string node_name = "rosnode_interface")
{
    m_exe = std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
    m_node = std::make_shared<rclcpp::Node>(node_name);
    m_chat_cli = m_node->create_client<ros2_vlmaps_interfaces::srv::LlmChat>("/chat_agent/user_text", 10);
    m_exe->add_node(m_node);
}

void OnSpeech::onRead(yarp::os::Bottle& b)
{
    try
    {
        auto text = b.get(0).asString();
        RCLCPP_INFO_STREAM(m_node->get_logger(), "Received string: " << text);
        // Create cli msg
        auto request = std::make_shared<ros2_vlmaps_interfaces::srv::LlmChat_Request>();
        request->text = text;
        auto future = m_chat_cli->async_send_request(request);
        if (m_exe->spin_until_future_complete(future, std::chrono::seconds(1)) == rclcpp::FutureReturnCode::TIMEOUT)
        {
            m_chat_cli->remove_pending_request(future);
        }
    return;
    }
    catch(const std::exception& e)
    {
        RCLCPP_ERROR_STREAM(m_node->get_logger(), "[onRead] Exception occurred: " << e.what());
    }
}

class SpeechBridge : public rclcpp::Node
{
private:
    // Topic name
    std::string m_speech_text_topic = "/llm_reasoning";
    // Output
    std::string m_out_yarp_port_name = "/vln_speech_bridge_node/text:o";
    std::string m_out_yarp_remote_port = "/speechSynthesizer_nws/text:i";
    yarp::os::BufferedPort<yarp::os::Bottle> m_out_port;
    // Input
    yarp::os::BufferedPort<yarp::os::Bottle> m_in_port;
    std::string m_in_yarp_port_name = "/vln_speech_bridge_node/text:i";
    std::string m_in_yarp_remote_port = "/speechTranscription_nws/text:o";
    // Subs
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr m_text_sub;
    
public:
    SpeechBridge();
    void callback(const std_msgs::msg::String::SharedPtr msg);
    OnSpeech yarp_wrap = OnSpeech();
};

SpeechBridge::SpeechBridge() : rclcpp::Node("vln_speech_bridge_node")
{
    m_text_sub = this->create_subscription<std_msgs::msg::String>(
                        m_speech_text_topic, 
                        10, 
                        [this](const std_msgs::msg::String::SharedPtr msg){this->callback(msg);});
    
    // Synth
    m_out_port.open(m_out_yarp_port_name);
    if (!yarp::os::Network::connect(m_out_yarp_port_name, m_out_yarp_remote_port))
    {
        RCLCPP_ERROR_STREAM(this->get_logger(), "Unable to connect " << m_out_yarp_port_name << " with " << m_out_yarp_remote_port);
    }
    // Transcriber
    m_in_port.useCallback(yarp_wrap);
    m_in_port.open(m_in_yarp_port_name);
    if (!yarp::os::Network::connect(m_in_yarp_remote_port, m_in_yarp_port_name))
    {
        RCLCPP_ERROR_STREAM(this->get_logger(), "Unable to connect " << m_in_yarp_port_name << " with " << m_in_yarp_remote_port);
    }
    
}

void SpeechBridge::callback(const std_msgs::msg::String::SharedPtr msg)
{
    std::string text = msg->data;
    RCLCPP_INFO_STREAM(this->get_logger(), "Received string: " << text);
    if (yarp::os::Network::isConnected(m_out_yarp_port_name, m_out_yarp_remote_port))
    {
        // Write to yarp port
        auto &out = m_out_port.prepare();
        out.clear();
        out.addString(text);
        m_out_port.write();
    }
    else
    {
        RCLCPP_WARN_STREAM(this->get_logger(), "Ports not connected: " << m_out_yarp_port_name << " and " << m_out_yarp_remote_port);
    }
    return;
}

int main(int argc, char** argv)
{
    rclcpp::init(argc, argv);
    yarp::os::Network yarp;
    if (rclcpp::ok())
    {
        rclcpp::executors::MultiThreadedExecutor exe;
        auto speech_node = std::make_shared<SpeechBridge>();
        exe.add_node(speech_node->get_node_base_interface());
        exe.spin();
        rclcpp::shutdown();
    }
    else
    {
        std::cout << "ROS2 not available. Shutting down node. \n";
    }
    
    return 0;
}