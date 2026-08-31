#ifndef _WEBSOCKET_PROTOCOL_H_
#define _WEBSOCKET_PROTOCOL_H_


#include "protocol.h"

#include <web_socket.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <esp_timer.h>

#define WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT (1 << 0)

// 常连保活：设备空闲时也保持 WebSocket，服务端才能主动下发工作事件。
// 服务端 close_connection_no_voice_time 默认 120s，固件 Protocol::IsTimeout 也是 120s，
// 30s 一次 ping 留出 4 倍余量。服务端需开启 enable_websocket_ping 才会回 pong。
#define WEBSOCKET_KEEPALIVE_TICK_MS 5000
#define WEBSOCKET_PING_INTERVAL_TICKS 6
#define WEBSOCKET_RECONNECT_INTERVAL_TICKS 2

class WebsocketProtocol : public Protocol {
public:
    WebsocketProtocol();
    ~WebsocketProtocol();

    bool Start() override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    bool OpenAudioChannel() override;
    void CloseAudioChannel() override;
    bool IsAudioChannelOpened() const override;

private:
    EventGroupHandle_t event_group_handle_;
    std::unique_ptr<WebSocket> websocket_;
    int version_ = 1;
    esp_timer_handle_t keepalive_timer_ = nullptr;
    int ping_ticks_ = 0;
    int reconnect_ticks_ = 0;
    // 上一次阻塞式重连尝试的结束时刻，给两次尝试之间设时间下限（见 OnKeepaliveTick）
    std::chrono::steady_clock::time_point last_connect_attempt_;

    bool Connect(bool report_error);
    void OnKeepaliveTick();
    void ParseServerHello(const cJSON* root);
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
};

#endif
