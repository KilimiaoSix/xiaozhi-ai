#include "wifi_board.h"
#include "audio/codecs/no_audio_codec.h"
#include "display/oled_display.h"
#include "system_reset.h"
#include "application.h"
#include "button.h"
#include "board_config.h"
#include "mcp_server.h"
#include "led/single_led.h"
#include "assets/lang_config.h"
#include "emoji_controller.h"
#include "servo_controller.h"
#include "emotion_response_controller.h"
#include "gesture_sensor.h"

#include <wifi_station.h>
#include <esp_log.h>
#include <string>
#include <vector>
#include <map>
#include <memory>
#include <algorithm>
#include <ctime>
#include <cstdlib>
#include <cstring>   // 添加cstring头文件，用于strchr()函数
#include <stdlib.h>  // 添加标准库头文件，用于rand()函数
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_random.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_lcd_panel_ops.h>
#include <driver/i2c.h>
#include <driver/i2c_master.h>  // 添加新版I2C驱动API头文件
#include <driver/gpio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>
#include <freertos/timers.h>
#include <lvgl.h>
#include <time.h>
#include <sys/time.h>

#define TAG "EmojiBoard"

LV_FONT_DECLARE(font_puhui_14_1);
LV_FONT_DECLARE(font_awesome_14_1);
// 番茄钟倒计时大字。30px 是已随 xiaozhi-fonts 链接进固件、且覆盖数字与冒号的
// 最大字号（4bpp，在单色屏上由 LVGL 阈值二值化，效果需真机确认）
LV_FONT_DECLARE(font_puhui_basic_30_4);

// 声明一个静态函数，用于处理AI回复
static void ProcessAIResponseTask(void* arg);

// 前向声明EmojiBoard类
class EmojiBoard;

// 全局变量，用于存储和访问EmojiBoard实例
EmojiBoard* g_board_instance = nullptr;

// 任务函数声明
static void StateMonitorTask(void* arg);

// MCP 动作工具可用的语义动作。
// 舵机动作是阻塞的（HeadNod/HeadRoll 可达 1~2 秒），而 MCP 工具回调跑在主事件
// 循环上（见 McpServer::DoToolCall 里的 app.Schedule），直接执行会拖住音频收发，
// 因此照 EmojiController 的做法，用队列 + 专用任务异步执行。
enum class RobotAction {
    kNod,
    kShake,
    kRoll,
    kLookLeft,
    kLookRight,
    kLookUp,
    kLookDown,
    // "保持"变体：转过去就停住不回中。流程四要小智转向同事并保持注视，
    // 而普通的 look_* 800ms 后强制回中，镜头上是"瞥一眼又扭回去"，明显穿帮。
    kHoldLeft,
    kHoldRight,
    kHoldUp,
    kHoldDown,
    kCenter
};

// 任务参数结构体，用于传递参数给静态任务函数
struct TaskParams {
    EmojiBoard* board;  // 使用EmojiBoard*代替void*
    EmotionResponseController* emotion_controller;
};

// 自定义OledDisplay类，用于捕获AI回复内容并触发表情和动作
class EmojiDisplay : public OledDisplay {
private:
    EmojiBoard* board_ = nullptr;
    bool processing_ai_response_ = false; // 添加标志，防止递归调用

public:
    // 重写SetEmotion方法，根据小智AI框架识别的表情触发我们自己的表情动画
    virtual void SetEmotion(const char* emotion) override;
    
    EmojiDisplay(EmojiBoard* board, esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel, 
                int width, int height, bool flip_x, bool flip_y) 
        : OledDisplay(io, panel, width, height, flip_x, flip_y), board_(board) {
        ESP_LOGI(TAG, "创建EmojiDisplay实例");
    }

    // 重写SetChatMessage方法，在显示AI回复时同时触发表情和动作
    void SetChatMessage(const char* role, const char* content) override;
};

class EmojiBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t display_i2c_bus_;
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    Display* display_ = nullptr;
    Button boot_button_;
    Button volume_up_button_;
    Button volume_down_button_;
    
    // 表情和舵机控制器
    EmojiController* emoji_controller_ = nullptr;
    ServoController* servo_controller_ = nullptr;
    
    // 手势识别传感器
    GestureSensor* gesture_sensor_ = nullptr;
    
    // MCP 动作工具的异步执行队列
    QueueHandle_t action_queue_ = nullptr;
    
    // 随机空闲动画的总开关。StateMonitorTask 会随对话状态开关随机动画，
    // 但那些都必须服从这个总开关——演示时要求机器人的每一次动作都是刻意的，
    // 否则观众分不清它是在回应事件还是自己在乱动。
    bool idle_animation_allowed_ = false;
    
    // 情感响应控制器
    EmotionResponseController* emotion_controller_ = nullptr;
    
    // 表情模式标志
    bool is_emoji_mode_ = false;
    
    // 对话模式屏幕
    lv_obj_t* chat_screen_ = nullptr;

    // ── 番茄钟画面（服务端 self.pomodoro.show 驱动，见 InitializeTools）──
    // 会话状态的权威在服务端，这里只保留渲染所需的最小状态
    lv_obj_t* pomodoro_screen_ = nullptr;
    lv_obj_t* pomo_phase_label_ = nullptr;
    lv_obj_t* pomo_round_label_ = nullptr;
    lv_obj_t* pomo_clock_label_ = nullptr;
    lv_obj_t* pomo_bar_ = nullptr;
    lv_timer_t* pomo_timer_ = nullptr;
    bool pomodoro_active_ = false;   // 有进行中的会话（含暂停）
    bool pomo_paused_ = false;
    int pomo_total_s_ = 0;
    // 运行中的到点时刻（esp_timer 时基）。paused 时画面停在推送来的剩余秒上，不自减
    int64_t pomo_deadline_us_ = 0;
    // SyncPomodoroScreen 的 100ms 计数：idle 稳定 2 秒才把画面收回来
    int pomo_idle_ticks_ = 0;

    // 上一次处理的AI回复
    std::string last_ai_response_;
    
    // 处理AI回复的方法
    void ProcessAIResponseInternal(const char* message) {
        // 如果消息为空，则不处理
        if (!message || message[0] == '\0') {
            return;
        }
        
        ESP_LOGI(TAG, "处理AI回复: %s", message);
        
        // 检查是否与上一次相同，如果相同则不处理
        if (last_ai_response_ == message) {
            return;
        }
        
        // 更新上一次处理的AI回复
        last_ai_response_ = message;
        
        // 检查是否包含特殊字符标记，如果有则立即处理
        if (message[0] && strchr("{}<>/\\$!?^*#~", message[0]) != nullptr) {
            ESP_LOGI(TAG, "检测到特殊字符标记: %c", message[0]);
            emotion_controller_->ProcessAIResponse(message);
        } else {
            // 如果没有特殊字符标记，则正常处理AI回复
            emotion_controller_->ProcessAIResponse(message);
        }
    }

    void InitializeDisplayI2c() {
        // 使用ESP-IDF v5.3.2的新版I2C驱动API
        i2c_master_bus_config_t bus_config = {};
        bus_config.i2c_port = I2C_NUM_0;
        bus_config.sda_io_num = DISPLAY_I2C_SDA_PIN;
        bus_config.scl_io_num = DISPLAY_I2C_SCL_PIN;
        bus_config.clk_source = I2C_CLK_SRC_DEFAULT;
        bus_config.glitch_ignore_cnt = 7;
        bus_config.flags.enable_internal_pullup = true;
        
        ESP_ERROR_CHECK(i2c_new_master_bus(&bus_config, &display_i2c_bus_));
        ESP_LOGI(TAG, "I2C总线初始化成功");
    }

    void InitializeSsd1306Display() {
        esp_lcd_panel_io_i2c_config_t io_config = {};
        io_config.dev_addr = DISPLAY_I2C_ADDR;
        io_config.on_color_trans_done = nullptr;
        io_config.user_ctx = nullptr;
        io_config.control_phase_bytes = 1;
        io_config.dc_bit_offset = 6;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        io_config.flags.dc_low_on_data = 0;
        io_config.flags.disable_control_phase = 0;
        io_config.scl_speed_hz = 400 * 1000;

        // 使用新版I2C面板IO API
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c_v2(display_i2c_bus_, &io_config, &panel_io_));
        ESP_LOGI(TAG, "LCD面板IO初始化成功");

        ESP_LOGI(TAG, "安装SSD1306驱动");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = -1;
        panel_config.bits_per_pixel = 1;

        esp_lcd_panel_ssd1306_config_t ssd1306_config = {
            .height = static_cast<uint8_t>(DISPLAY_HEIGHT),
        };
        panel_config.vendor_config = &ssd1306_config;

        ESP_ERROR_CHECK(esp_lcd_new_panel_ssd1306(panel_io_, &panel_config, &panel_));
        ESP_LOGI(TAG, "SSD1306驱动安装成功");

        ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_));
        
        if (esp_lcd_panel_init(panel_) != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialize display");
            display_ = new NoDisplay();
            return;
        }

        ESP_LOGI(TAG, "Turning display on");
        ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_, true));

        // 使用自定义的EmojiDisplay类
        display_ = new EmojiDisplay(this, panel_io_, panel_, DISPLAY_WIDTH, DISPLAY_HEIGHT, true, true);

        // 保存对话模式屏幕的引用
        chat_screen_ = lv_screen_active();
    }

    void InitializeButtons() {
        boot_button_.OnClick([this]() {
            // 无论在哪种模式下，短按BOOT按钮都进入录音状态
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateStarting && !WifiStation::GetInstance().IsConnected()) {
                ResetWifiConfiguration();
            }
            app.ToggleChatState();
        });

        boot_button_.OnLongPress([this]() {
            if (is_emoji_mode_) {
                ExitEmojiMode();
            } else {
                EnterEmojiMode(true);
            }
        });

        // 双击/三击：番茄钟控制。固件只上报"按了几下"，语义（双击=开始/暂停/继续、
        // 三击=取消）定死在服务端 listenMessageHandler 的 BUTTON_COMMANDS 里，
        // 改玩法不用重烧固件；文本必须逐字对上服务端的键名。
        // 回调跑在 esp_timer 任务上，SendDetectedText 自查通道状态后 Schedule 回
        // 主循环发送（同 gesture_sensor.cc 的上报路径），这里只做一次轻量调用。
        // 单击的 180ms 判定延迟是 iot_button 状态机固有行为，注册双击并不会引入。
        boot_button_.OnDoubleClick([]() {
            Application::GetInstance().SendDetectedText("[button]boot_double");
        });

        boot_button_.OnMultipleClick([]() {
            Application::GetInstance().SendDetectedText("[button]boot_triple");
        }, 3);

        volume_up_button_.OnClick([this]() {
            // 无论在哪种模式下，音量+按钮都控制音量增加
            auto codec = GetAudioCodec();
            auto volume = codec->output_volume() + 10;
            if (volume > 100) {
                volume = 100;
            }
            codec->SetOutputVolume(volume);
            GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
        });

        volume_up_button_.OnLongPress([this]() {
            // 无论在哪种模式下，长按音量+按钮都将音量设为最大
            GetAudioCodec()->SetOutputVolume(40);  // 40 兼顾可听清与打断可用性（满音量下单麦无AEC导致语音打断物理不可用），用户可用音量键随时调
            GetDisplay()->ShowNotification(Lang::Strings::MAX_VOLUME);
        });

        volume_down_button_.OnClick([this]() {
            // 无论在哪种模式下，音量-按钮都控制音量减小
            auto codec = GetAudioCodec();
            auto volume = codec->output_volume() - 10;
            if (volume < 0) {
                volume = 0;
            }
            codec->SetOutputVolume(volume);
            GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
        });

        volume_down_button_.OnLongPress([this]() {
            // 无论在哪种模式下，长按音量-按钮都将音量设为0
            GetAudioCodec()->SetOutputVolume(0);
            GetDisplay()->ShowNotification(Lang::Strings::MUTED);
        });
    }

    void InitializeIot() {
        // 新的MCP架构不再需要手动初始化Thing，由框架自动管理
        ESP_LOGI(TAG, "新版MCP架构已自动管理设备功能");
    }
    
    // 启动动作执行任务，把阻塞的舵机动作挪出主事件循环
    void InitializeActionTask() {
        action_queue_ = xQueueCreate(4, sizeof(RobotAction));
        if (action_queue_ == nullptr) {
            ESP_LOGE(TAG, "动作队列创建失败，MCP动作工具将不可用");
            return;
        }
        
        xTaskCreate([](void* arg) {
            auto board = (EmojiBoard*)arg;
            RobotAction action;
            while (true) {
                if (xQueueReceive(board->action_queue_, &action, portMAX_DELAY) != pdPASS) {
                    continue;
                }
                auto servo = board->servo_controller_;
                if (servo == nullptr) {
                    continue;
                }
                switch (action) {
                    case RobotAction::kNod:
                        servo->HeadNod();
                        break;
                    case RobotAction::kShake:
                        servo->HeadShake();
                        break;
                    case RobotAction::kRoll:
                        servo->HeadRoll();
                        break;
                    case RobotAction::kLookLeft:
                        servo->HeadLeft(30);
                        vTaskDelay(pdMS_TO_TICKS(800));
                        servo->HeadCenter();
                        break;
                    case RobotAction::kLookRight:
                        servo->HeadRight(30);
                        vTaskDelay(pdMS_TO_TICKS(800));
                        servo->HeadCenter();
                        break;
                    case RobotAction::kLookUp:
                        servo->HeadUp(25);
                        vTaskDelay(pdMS_TO_TICKS(800));
                        servo->HeadCenter();
                        break;
                    case RobotAction::kLookDown:
                        servo->HeadDown(25);
                        vTaskDelay(pdMS_TO_TICKS(800));
                        servo->HeadCenter();
                        break;
                    case RobotAction::kHoldLeft:
                        servo->HeadLeft(30);
                        break;
                    case RobotAction::kHoldRight:
                        servo->HeadRight(30);
                        break;
                    case RobotAction::kHoldUp:
                        servo->HeadUp(25);
                        break;
                    case RobotAction::kHoldDown:
                        servo->HeadDown(25);
                        break;
                    case RobotAction::kCenter:
                        servo->HeadCenter();
                        break;
                }
            }
        }, "RobotAction", 4096, this, 2, nullptr);
        
        ESP_LOGI(TAG, "动作执行任务已启动");
    }
    
    // 动作名转枚举并入队，未知动作返回 false
    bool EnqueueAction(const std::string& name) {
        static const std::map<std::string, RobotAction> kActions = {
            {"nod",        RobotAction::kNod},
            {"shake",      RobotAction::kShake},
            {"roll",       RobotAction::kRoll},
            {"look_left",  RobotAction::kLookLeft},
            {"look_right", RobotAction::kLookRight},
            {"look_up",    RobotAction::kLookUp},
            {"look_down",  RobotAction::kLookDown},
            {"hold_left",  RobotAction::kHoldLeft},
            {"hold_right", RobotAction::kHoldRight},
            {"hold_up",    RobotAction::kHoldUp},
            {"hold_down",  RobotAction::kHoldDown},
            {"center",     RobotAction::kCenter},
        };
        
        auto it = kActions.find(name);
        if (it == kActions.end()) {
            return false;
        }
        if (action_queue_ == nullptr) {
            ESP_LOGW(TAG, "动作队列不可用，丢弃动作: %s", name.c_str());
            return false;
        }
        
        RobotAction action = it->second;
        // 绝不阻塞：EnqueueAction 跑在主事件循环上（McpServer::DoToolCall 的 app.Schedule）。
        // 队列满时丢最旧的而不是丢新的——动作是即时反馈，堆积的旧动作播出来对不上画面，
        // 而刚触发的这个才是当前想表达的意图。
        if (xQueueSend(action_queue_, &action, 0) != pdPASS) {
            RobotAction discarded;
            if (xQueueReceive(action_queue_, &discarded, 0) == pdPASS) {
                ESP_LOGW(TAG, "动作队列已满，丢弃最旧动作以让位给: %s", name.c_str());
            }
            if (xQueueSend(action_queue_, &action, 0) != pdPASS) {
                ESP_LOGW(TAG, "动作入队失败: %s", name.c_str());
                return false;
            }
        }
        ESP_LOGI(TAG, "已入队动作: %s", name.c_str());
        return true;
    }
    
    void InitializeTools() {
        auto& mcp_server = McpServer::GetInstance();
        
        mcp_server.AddTool("self.robot.play_action",
            "Play a physical head action on the desktop robot, for embodied feedback.\n"
            "Available actions: `nod` (acknowledge / confirm / say yes), "
            "`shake` (deny / reject / say no), `roll` (celebrate a finished task), "
            "`look_left`, `look_right`, `look_up`, `look_down` (glance and return), "
            "`hold_left`, `hold_right`, `hold_up`, `hold_down` (turn and STAY there — "
            "use these to keep facing someone), `center` (reset posture).\n"
            "The action runs asynchronously and returns immediately.",
            PropertyList({
                Property("action", kPropertyTypeString)
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                auto action = properties["action"].value<std::string>();
                if (!EnqueueAction(action)) {
                    throw std::runtime_error("Unknown or undeliverable action: " + action);
                }
                return true;
            });
        
        mcp_server.AddTool("self.robot.set_emotion",
            "Set the facial expression shown on the robot's screen. "
            "It also drives the matching head movement.\n"
            "Available emotions: neutral, happy, laughing, funny, sad, crying, angry, "
            "surprised, shocked, confused, thinking, embarrassed, sleepy, winking, cool, "
            "confident, relaxed, loving, kissy, delicious, silly.",
            PropertyList({
                Property("emotion", kPropertyTypeString)
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                auto emotion = properties["emotion"].value<std::string>();
                if (display_ == nullptr) {
                    throw std::runtime_error("Display is not ready");
                }
                // 直接复用 EmojiDisplay::SetEmotion 里已有的映射表，避免两处维护
                display_->SetEmotion(emotion.c_str());
                return true;
            });
        
        mcp_server.AddTool("self.robot.set_idle_animation",
            "Enable or disable the robot's idle fidget animation (a random head "
            "movement every ~10 seconds). Disabled by default so that every movement "
            "the robot makes is a deliberate response. Turn it on to make the robot "
            "look alive while standing by.",
            PropertyList({
                Property("enabled", kPropertyTypeBoolean)
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                bool enabled = properties["enabled"].value<bool>();
                if (emoji_controller_ == nullptr) {
                    throw std::runtime_error("Emoji controller is not ready");
                }
                SetIdleAnimationAllowed(enabled);
                return true;
            });

        // 服务端 pomodoro_manager 在每次相位切换/暂停/恢复时调用（工具名经服务端
        // sanitize 后是 self_pomodoro_show）。参数契约对齐 pomodoro_manager._show_args。
        mcp_server.AddTool("self.pomodoro.show",
            "Render the pomodoro countdown screen on the OLED. Driven by the server "
            "on every phase change / pause / resume; the device counts down locally "
            "at 1Hz from `remaining_s` and freezes at 00:00 until the next push. "
            "`phase` is one of `focus`, `short_break`, `long_break`, `idle`; "
            "`idle` dismisses the screen and returns to the normal face.",
            PropertyList({
                Property("phase", kPropertyTypeString),
                Property("paused", kPropertyTypeBoolean, false),
                Property("remaining_s", kPropertyTypeInteger, 0, 0, 86400),
                Property("total_s", kPropertyTypeInteger, 0, 0, 86400),
                Property("round", kPropertyTypeInteger, 0, 0, 99),
                Property("total_rounds", kPropertyTypeInteger, 4, 1, 99),
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                auto phase = properties["phase"].value<std::string>();
                if (phase != "focus" && phase != "short_break" &&
                    phase != "long_break" && phase != "idle") {
                    // 经 ReplyError 变成 JSON-RPC error，服务端把它包成通用
                    // Exception：重试一次后放弃（服务端不重试的 ValueError 分支
                    // 只接得住它本地的"工具不存在"，接不住这里的 throw）
                    throw std::runtime_error("Unknown phase: " + phase);
                }
                ShowPomodoro(phase,
                             properties["paused"].value<bool>(),
                             properties["remaining_s"].value<int>(),
                             properties["total_s"].value<int>(),
                             properties["round"].value<int>(),
                             properties["total_rounds"].value<int>());
                return true;
            });

        ESP_LOGI(TAG, "已注册机器人动作、表情与番茄钟MCP工具");
    }
    
    // 状态机想要的随机动画状态，与总开关求与后再落到控制器
    void ApplyIdleAnimation(bool wanted) {
        if (emoji_controller_ == nullptr) {
            return;
        }
        emoji_controller_->SetRandomAnimationEnabled(wanted && idle_animation_allowed_);
    }

    void SetIdleAnimationAllowed(bool allowed) {
        idle_animation_allowed_ = allowed;
        ESP_LOGI(TAG, "随机空闲动画总开关: %s", allowed ? "开" : "关");
        // 立即生效；后续状态机切换会再按对话状态收敛
        ApplyIdleAnimation(allowed);
    }

    // 进入表情模式。announce=false 用于开机静默进入，不弹通知。
    // 表情动画和舵机联动都依赖 emoji_screen_ 对象存在（见 emoji_controller.cc 各
    // Execute*Animation 开头的空指针检查），不进这个模式的话推送来的表情全是哑的。
    void EnterEmojiMode(bool announce) {
        is_emoji_mode_ = true;
        SwitchScreen(true);
        emoji_controller_->StartBlinkTimer();
        emoji_controller_->EyeCenter();
        if (announce) {
            GetDisplay()->ShowNotification("表情模式");
        }
        servo_controller_->HeadCenter();
    }

    void ExitEmojiMode() {
        is_emoji_mode_ = false;
        emoji_controller_->StopBlinkTimer();
        SwitchScreen(false);
        GetDisplay()->ShowNotification("对话模式");
        emoji_controller_->CleanupEmojiScreen();
        servo_controller_->HeadCenter();
    }

    // 切换屏幕
    void SwitchScreen(bool to_emoji_mode) {
        DisplayLockGuard lock(display_);
        if (to_emoji_mode) {
            lv_obj_t* emoji_screen = emoji_controller_->GetEmojiScreen();
            if (!emoji_screen) {
                emoji_screen = emoji_controller_->CreateEmojiScreen();
            }
            lv_scr_load(emoji_screen);
        } else {
            lv_scr_load(chat_screen_);
        }
    }

    // ------------------------------------------------------------ 番茄钟画面
    // 计时权威在服务端：相位切换/暂停/恢复时各推一次 self.pomodoro.show，设备只渲染。
    // 拿到 remaining_s 后本地 1Hz 自减，走到 00:00 停住等下一次推送——WiFi 抖动
    // 只会让画面短暂不同步，不会让两端轮次各走各的。

    static void PomodoroTimerCb(lv_timer_t* timer) {
        auto board = static_cast<EmojiBoard*>(lv_timer_get_user_data(timer));
        board->PomodoroTick();
    }

    // lv_timer 回调由 LVGL 任务在 lv_timer_handler 持锁期间调用（同 lvgl_gif.cc 的
    // 先例），直接摸 lv 对象是安全的，不需要再拿 DisplayLockGuard。
    void PomodoroTick() {
        if (!pomodoro_active_ || pomo_paused_) {
            return;
        }
        int64_t now_us = esp_timer_get_time();
        int remaining = 0;
        if (pomo_deadline_us_ > now_us) {
            // 向上取整，与服务端 _ceil_seconds 对齐：刚推送时显示 25:00 而不是 24:59
            remaining = (int)((pomo_deadline_us_ - now_us + 999999) / 1000000);
        }
        UpdatePomodoroCountdown(remaining);
    }

    // 只刷新每秒变化的部分；相位/轮次文本在 ShowPomodoro 里设置。调用方必须持有 LVGL 锁
    void UpdatePomodoroCountdown(int remaining_s) {
        char clock_text[16];
        snprintf(clock_text, sizeof(clock_text), "%02d:%02d",
                 remaining_s / 60, remaining_s % 60);
        lv_label_set_text(pomo_clock_label_, clock_text);
        if (pomo_total_s_ > 0) {
            lv_bar_set_value(pomo_bar_, pomo_total_s_ - remaining_s, LV_ANIM_OFF);
        }
    }

    // 懒创建，同 emoji_screen_ 的做法。调用方必须持有 LVGL 锁
    void CreatePomodoroScreen() {
        pomodoro_screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(pomodoro_screen_, lv_color_black(), 0);

        // 顶行：相位（左）+ 轮次（右）
        pomo_phase_label_ = lv_label_create(pomodoro_screen_);
        lv_obj_set_style_text_font(pomo_phase_label_, &font_puhui_14_1, 0);
        lv_obj_set_style_text_color(pomo_phase_label_, lv_color_white(), 0);
        lv_obj_align(pomo_phase_label_, LV_ALIGN_TOP_LEFT, 2, 0);
        lv_label_set_text(pomo_phase_label_, "");

        pomo_round_label_ = lv_label_create(pomodoro_screen_);
        lv_obj_set_style_text_font(pomo_round_label_, &font_puhui_14_1, 0);
        lv_obj_set_style_text_color(pomo_round_label_, lv_color_white(), 0);
        lv_obj_align(pomo_round_label_, LV_ALIGN_TOP_RIGHT, -2, 0);
        lv_label_set_text(pomo_round_label_, "");

        // 中央大字 MM:SS
        pomo_clock_label_ = lv_label_create(pomodoro_screen_);
        lv_obj_set_style_text_font(pomo_clock_label_, &font_puhui_basic_30_4, 0);
        lv_obj_set_style_text_color(pomo_clock_label_, lv_color_white(), 0);
        lv_obj_align(pomo_clock_label_, LV_ALIGN_CENTER, 0, 2);
        lv_label_set_text(pomo_clock_label_, "00:00");

        // 底部进度条：已走过的时间从左往右填满。单色屏上用白边框 + 白色指示条
        pomo_bar_ = lv_bar_create(pomodoro_screen_);
        lv_obj_set_size(pomo_bar_, 124, 7);
        lv_obj_align(pomo_bar_, LV_ALIGN_BOTTOM_MID, 0, -1);
        lv_obj_set_style_bg_opa(pomo_bar_, LV_OPA_TRANSP, LV_PART_MAIN);
        lv_obj_set_style_border_width(pomo_bar_, 1, LV_PART_MAIN);
        lv_obj_set_style_border_color(pomo_bar_, lv_color_white(), LV_PART_MAIN);
        lv_obj_set_style_radius(pomo_bar_, 2, LV_PART_MAIN);
        lv_obj_set_style_pad_all(pomo_bar_, 1, LV_PART_MAIN);
        lv_obj_set_style_bg_color(pomo_bar_, lv_color_white(), LV_PART_INDICATOR);
        lv_obj_set_style_radius(pomo_bar_, 1, LV_PART_INDICATOR);

        // 常驻 1Hz 定时器，回调按 active/paused 自行早退，不做 pause/resume 状态管理
        pomo_timer_ = lv_timer_create(PomodoroTimerCb, 1000, this);
    }

    // 服务端推送落地。跑在主事件循环上（McpServer::DoToolCall 的 app.Schedule）
    void ShowPomodoro(const std::string& phase, bool paused, int remaining_s,
                      int total_s, int round, int total_rounds) {
        DisplayLockGuard lock(display_);

        if (phase == "idle") {
            // 服务端 stop（或重启后的收屏兜底）：回到进番茄钟之前的模式画面
            pomodoro_active_ = false;
            if (pomodoro_screen_ != nullptr && lv_scr_act() == pomodoro_screen_) {
                SwitchScreen(is_emoji_mode_);
            }
            return;
        }

        if (pomodoro_screen_ == nullptr) {
            CreatePomodoroScreen();
        }

        pomo_paused_ = paused;
        pomo_total_s_ = total_s;
        pomo_deadline_us_ = esp_timer_get_time() + (int64_t)remaining_s * 1000000;

        const char* phase_name = "专注";
        if (phase == "short_break") {
            phase_name = "短休";
        } else if (phase == "long_break") {
            phase_name = "长休";
        }
        if (paused) {
            lv_label_set_text_fmt(pomo_phase_label_, "%s 已暂停", phase_name);
        } else {
            lv_label_set_text(pomo_phase_label_, phase_name);
        }
        lv_label_set_text_fmt(pomo_round_label_, "%d/%d", round, total_rounds);
        if (total_s > 0) {
            lv_bar_set_range(pomo_bar_, 0, total_s);
        }
        UpdatePomodoroCountdown(remaining_s);

        pomodoro_active_ = true;

        // 设备空闲才立即抢屏；对话中让屏，等 SyncPomodoroScreen 在回 idle 后收复
        if (Application::GetInstance().GetDeviceState() == kDeviceStateIdle &&
            lv_scr_act() != pomodoro_screen_) {
            lv_scr_load(pomodoro_screen_);
        }
    }

    // StateMonitorTask 每 100ms 调一次：对话打断让屏，回到 idle 后恢复倒计时画面。
    // 不挂在下面那个"对话结束"判定块里——那个分支只在状态转换的单个 tick 上比较
    // 3 秒窗口，而 speaking→idle 时时间戳刚被重置，条件常常永远不成立。
    void SyncPomodoroScreen(DeviceState state) {
        if (pomodoro_screen_ == nullptr) {
            return;  // 从未进过番茄钟，不必每 100ms 白拿一次锁
        }
        DisplayLockGuard lock(display_);
        // active 必须持锁后再判：ShowPomodoro("idle") 可能在预检查与拿锁的间隙把
        // 会话收掉（服务端 stop 恰好落在让屏窗口时），检查-加载不原子就会把
        // 已结束的画面重新载入并永久卡屏
        if (!pomodoro_active_) {
            pomo_idle_ticks_ = 0;
            if (lv_scr_act() == pomodoro_screen_) {
                // stop 落在让屏窗口时 ShowPomodoro 那边收不到屏，这里兜底
                SwitchScreen(is_emoji_mode_);
            }
            return;
        }
        // listening 与 idle 同等对待：auto-listen 模式下环境人声会让设备长期停在
        // listening（说完一轮也直接回 listening，不经过 idle），而 listening 的对话屏
        // 只有一张待机脸、没有任何信息——按旧策略它会把倒计时顶掉且收回条件
        // （仅认 idle）永远凑不齐，专注中的用户看到的就是"倒计时时隐时现"。
        // speaking/connecting 等仍让屏：播报字幕和连接状态是用户需要看见的。
        if (state == kDeviceStateIdle || state == kDeviceStateListening) {
            if (lv_scr_act() == pomodoro_screen_) {
                pomo_idle_ticks_ = 0;
                return;
            }
            // 稳定 2 秒再收回画面：给对话收尾的 neutral 表情留出镜头，
            // 也让长按切出去看脸的用户能看上一眼
            if (++pomo_idle_ticks_ >= 20) {
                pomo_idle_ticks_ = 0;
                ESP_LOGI(TAG, "番茄钟画面收回 (state=%d)", (int)state);
                lv_scr_load(pomodoro_screen_);
            }
        } else {
            pomo_idle_ticks_ = 0;
            if (lv_scr_act() == pomodoro_screen_) {
                // 播报/连接期间让屏给表情与字幕
                ESP_LOGI(TAG, "番茄钟让屏 (state=%d)", (int)state);
                SwitchScreen(is_emoji_mode_);
            }
        }
    }

    // 相位到点的庆祝和事件提醒走 Alert→SetEmotion，不经过番茄钟推送；此时若
    // 倒计时画面在前台，表情动画画在 emoji_screen_ 上根本看不见。让屏给表情脸，
    // SyncPomodoroScreen 会在 idle 稳定 2 秒后自动把倒计时收回来。
    // neutral 除外：WS 重连时 DismissAlert 会推 neutral 兜底，不该打断倒计时。
    void YieldPomodoroScreenForAlert(const char* emotion) {
        if (emotion == nullptr || emotion[0] == '\0' ||
            strcmp(emotion, "neutral") == 0) {
            return;
        }
        DisplayLockGuard lock(display_);
        if (!pomodoro_active_ || pomodoro_screen_ == nullptr ||
            lv_scr_act() != pomodoro_screen_) {
            return;
        }
        // 表情动画只存在于 emoji_screen_，对话模式下也得切到脸才看得见
        SwitchScreen(true);
        pomo_idle_ticks_ = 0;
    }

    // 声明静态任务函数为友元函数，使其可以访问私有成员
    friend void StateMonitorTask(void* arg);

    // 声明EmojiDisplay为友元类，使其能够访问EmojiBoard的私有成员
    friend class EmojiDisplay;

    // 声明ProcessAIResponseTask为友元函数，使其能够访问私有成员
    friend void ProcessAIResponseTask(void* arg);

public:
    EmojiBoard() :
        boot_button_(BOOT_BUTTON_PIN),
        volume_up_button_(VOLUME_UP_BUTTON_PIN),
        volume_down_button_(VOLUME_DOWN_BUTTON_PIN) {
#if !SERVO_ONLY_DEBUG
        InitializeDisplayI2c();
        InitializeSsd1306Display();
#else
        display_ = new NoDisplay();
        ESP_LOGI(TAG, "SERVO_ONLY_DEBUG 模式：跳过显示屏初始化");
#endif
        
        // 创建表情控制器和舵机控制器
        emoji_controller_ = new EmojiController(display_);
        servo_controller_ = new ServoController();
        
        // 初始化控制器
        emoji_controller_->Initialize();
        servo_controller_->Initialize();
        
        // 设置表情控制器的舵机控制器
        emoji_controller_->SetServoController(servo_controller_);
        
        // 创建并初始化情感响应控制器
        emotion_controller_ = new EmotionResponseController(emoji_controller_, servo_controller_, GetAudioCodec());
        emotion_controller_->Initialize();

#if SERVO_ONLY_DEBUG
        // 在调试模式下，创建一个循环任务来持续演示各种动作，方便验证和优化
        xTaskCreate([](void* arg) {
            auto board = (EmojiBoard*)arg;
            auto sc = board->servo_controller_;
            ESP_LOGI(TAG, "启动增强型循环调试任务 (含随机动作与日志)...");
            
            while (true) {
                // 1. 标准测试序列
                ESP_LOGI(TAG, "[Debug] 执行自检：居中 -> 摇头 -> 点头");
                sc->HeadCenter();
                vTaskDelay(pdMS_TO_TICKS(1000));
                sc->HeadShake(10);
                vTaskDelay(pdMS_TO_TICKS(1000));
                sc->HeadNod(10);
                vTaskDelay(pdMS_TO_TICKS(1500));

                // 2. 随机看向不同方向测试 (左/右/中心)
                ESP_LOGI(TAG, "[Debug] 执行序列：看左 -> 居中 -> 看右 -> 居中");
                
                ESP_LOGI(TAG, "[Action] 看向左侧 (-30)");
                sc->HeadMove(-30, 0, 15);
                vTaskDelay(pdMS_TO_TICKS(1000));
                
                ESP_LOGI(TAG, "[Action] 回到中心");
                sc->HeadCenter(15);
                vTaskDelay(pdMS_TO_TICKS(800));
                
                ESP_LOGI(TAG, "[Action] 看向右侧 (+30)");
                sc->HeadMove(30, 0, 15);
                vTaskDelay(pdMS_TO_TICKS(1000));
                
                ESP_LOGI(TAG, "[Action] 再次居中");
                sc->HeadCenter(15);
                vTaskDelay(pdMS_TO_TICKS(2000));
                
                // 3. 随机小幅度张望模拟
                ESP_LOGI(TAG, "[Debug] 执行序列：随机张望");
                for(int i=0; i<3; i++) {
                    int rx = (rand() % 40) - 20; // -20 to 20
                    int ry = (rand() % 40) - 20; // 增大随机张望幅度
                    ESP_LOGI(TAG, "[Action] 随机张望到: X=%d, Y=%d", rx, ry);
                    sc->HeadMove(rx, ry, 20);
                    vTaskDelay(pdMS_TO_TICKS(500));
                }
                sc->HeadCenter(15);
                vTaskDelay(pdMS_TO_TICKS(5000));
            }
        }, "servo_debug", 4096, this, 1, NULL);
#else
        // 正常模式下仅执行一次开机自检
        servo_controller_->HeadCenter();
        vTaskDelay(pdMS_TO_TICKS(500));
        servo_controller_->HeadShake(10);
        vTaskDelay(pdMS_TO_TICKS(500));
        servo_controller_->HeadNod(10);
        vTaskDelay(pdMS_TO_TICKS(500));
        servo_controller_->HeadCenter();
#endif
        
        // 创建并初始化手势识别传感器
        gesture_sensor_ = new GestureSensor();
        
        // 创建I2C互斥锁用于总线共享协调
        SemaphoreHandle_t i2c_mutex = xSemaphoreCreateMutex();
        if (i2c_mutex) {
            GestureSensor::SetI2CMutex(i2c_mutex);
            ESP_LOGI(TAG, "I2C互斥锁创建成功，启用总线共享协调");
        } else {
            ESP_LOGW(TAG, "I2C互斥锁创建失败");
        }
        
#if !SERVO_ONLY_DEBUG
        // 设置I2C总线句柄（与显示屏共用）
        gesture_sensor_->SetI2CBus(display_i2c_bus_);
        if (gesture_sensor_->Initialize()) {
            // 设置舵机控制器
            gesture_sensor_->SetServoController(servo_controller_);
            // 设置表情控制器
            gesture_sensor_->SetEmojiController(emoji_controller_);
            // 启动手势检测任务
            gesture_sensor_->StartGestureTask();
            ESP_LOGI(TAG, "PAJ7620U2手势识别传感器初始化成功");
        } else {
            ESP_LOGE(TAG, "PAJ7620U2手势识别传感器初始化失败");
            delete gesture_sensor_;
            gesture_sensor_ = nullptr;
        }
#else
        ESP_LOGI(TAG, "SERVO_ONLY_DEBUG 模式：跳过手势传感器初始化");
#endif
        
        // 新版MCP架构不再需要设置全局情感控制器指针
        // iot::SetGlobalEmotionController(emotion_controller_);
        
        InitializeButtons();
        InitializeIot();
        InitializeActionTask();
        InitializeTools();
        
        // 创建一个任务来监听设备状态变化
        TaskParams* state_params = new TaskParams();
        state_params->emotion_controller = emotion_controller_;
        state_params->board = this;
        xTaskCreate(StateMonitorTask, "StateMonitor", 8192, state_params, 1, NULL);
        
        // 随机空闲动画默认关闭（idle_animation_allowed_ 默认 false），
        // 需要时用 self.robot.set_idle_animation 运行时打开，不必重烧固件。
        
        // 开机直接进入表情模式：桌宠应当一直有张脸，
        // 而且服务端推送来的表情/动作都要求 emoji_screen_ 已创建
        EnterEmojiMode(false);
        
        // 将自身实例赋值给全局变量
        g_board_instance = this;
    }

    ~EmojiBoard() {
        // 停止并清理手势识别传感器
        if (gesture_sensor_) {
            gesture_sensor_->StopGestureTask();
            delete gesture_sensor_;
        }
        
        delete emoji_controller_;
        delete servo_controller_;
        delete emotion_controller_;
        // 清除全局变量
        g_board_instance = nullptr;
    }

    virtual Led* GetLed() override {
        static SingleLed led(LED_PIN);
        return &led;
    }

    virtual AudioCodec* GetAudioCodec() override {
        static NoAudioCodecSimplex audio_codec(AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            I2S_SPEAKER_BCLK_PIN, I2S_SPEAKER_WS_PIN, I2S_DATA_OUT_PIN,
            I2S_MIC_SCK_PIN, I2S_MIC_WS_PIN, I2S_DATA_IN_PIN);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }
    
    /**
     * @brief 处理用户输入的命令
     * @param message 用户输入的消息
     */
    void ProcessUserCommand(const char* message) {
        if (!message || message[0] == '\0') {
            return;
        }
        
        // 直接使用情感响应控制器处理用户命令
        if (emotion_controller_) {
            // 检查是否是表情动作命令
            if (emotion_controller_->ProcessEmotionCommand(message)) {
                ESP_LOGI(TAG, "用户输入的表情动作命令已处理: %s", message);
                return;
            }
            
            // 检查是否是音量控制命令
            if (emotion_controller_->ProcessVolumeCommand(message)) {
                ESP_LOGI(TAG, "用户输入的音量控制命令已处理: %s", message);
                return;
            }
        }
    }

    // 处理AI回复的公共方法，可以被外部调用
    void ProcessAIResponse(const char* message) {
        ProcessAIResponseInternal(message);
    }
};

// 在EmojiBoard类定义后实现EmojiDisplay::SetChatMessage方法
void EmojiDisplay::SetChatMessage(const char* role, const char* content) {
    // 检查是否正在处理AI回复，避免递归调用
    if (processing_ai_response_) {
        // 如果正在处理AI回复，只调用父类方法显示消息，不进行额外处理
        OledDisplay::SetChatMessage(role, content);
        return;
    }
    
    // 设置标志，表示正在处理AI回复
    processing_ai_response_ = true;
    
    // 首先调用父类方法显示消息
    OledDisplay::SetChatMessage(role, content);
    
    // 如果是AI回复，则处理内容
    if (role && strcmp(role, "assistant") == 0 && content && content[0] != '\0') {
        ESP_LOGI(TAG, "EmojiDisplay捕获AI回复: %s", content);
        
        // 调用EmojiBoard的ProcessAIResponse方法处理AI回复
        if (board_ && board_->emotion_controller_) {
            // 简化处理逻辑，只更新last_ai_response_并调用ProcessAIResponse
            if (board_->last_ai_response_ != content) {
                board_->last_ai_response_ = content;
                
                // 检查是否包含特殊字符标记，如果有则直接处理
                if (content[0] && strchr("{}<>/\\$!?^*#~", content[0]) != nullptr) {
                    ESP_LOGI(TAG, "检测到特殊字符标记: %c", content[0]);
                    
                    // 直接调用情感控制器处理特殊字符
                    board_->emotion_controller_->ProcessAIResponse(content);
                    
                    // 不再创建额外的任务处理AI回复，避免栈溢出
                } else {
                    // 使用后台任务处理AI回复，避免在主循环中处理复杂逻辑
                    // 增加栈大小，避免栈溢出
                    xTaskCreate(ProcessAIResponseTask, "ai_response", 8192, strdup(content), 1, NULL);
                }
            }
        }
    }
    
    // 重置标志
    processing_ai_response_ = false;
}

// 监听设备状态的静态任务函数
static void StateMonitorTask(void* arg) {
    TaskParams* params = static_cast<TaskParams*>(arg);
    EmojiBoard* board = params->board;
    if (!board) {
        ESP_LOGE(TAG, "Board instance is null");
        vTaskDelete(nullptr);
        return;
    }
    
    // 获取情感控制器
    auto* emotion_controller = params->emotion_controller;
    if (!emotion_controller) {
        ESP_LOGE(TAG, "Emotion controller is null");
        vTaskDelete(nullptr);
        return;
    }
    
    // 初始化随机数生成器
    srand(time(nullptr));
    
    // 初始化变量
    DeviceState last_state = kDeviceStateIdle;
    TickType_t last_speak_end_time = 0;
    bool in_conversation = false;
    
    // 确保初始状态下随机动画是启用的
    if (board->emoji_controller_) {
        board->ApplyIdleAnimation(true);
        ESP_LOGI(TAG, "初始化：随机表情动画交由总开关决定");
    }
    
    // 监控设备状态
    while (true) {
        // 获取当前设备状态
        DeviceState current_state = Application::GetInstance().GetDeviceState();
        
        // 如果设备状态发生变化，记录日志
        if (current_state != last_state) {
            ESP_LOGI(TAG, "设备状态变化: %d -> %d", last_state, current_state);
        }
        
        // 如果设备状态从idle变为speaking或listening，说明对话开始
        if (last_state == kDeviceStateIdle && 
            (current_state == kDeviceStateSpeaking || current_state == kDeviceStateListening)) {
            // 标记为对话中
            in_conversation = true;
            
            // 停止随机表情动画
            if (board->emoji_controller_) {
                board->ApplyIdleAnimation(false);
                board->emoji_controller_->ClearAnimationQueue();
                ESP_LOGI(TAG, "对话开始，停止随机表情动画");
            }
            
            // 如果是AI开始回复
            if (current_state == kDeviceStateSpeaking) {
                // 随机触发一种积极情感（开心或惊讶）
                static const char* positive_emotions[] = {
                    "happy", "surprise"
                };
                int random_index = rand() % (sizeof(positive_emotions) / sizeof(positive_emotions[0]));
                emotion_controller->TriggerEmotion(positive_emotions[random_index]);
                
                ESP_LOGI(TAG, "AI开始回复，触发积极情感: %s", positive_emotions[random_index]);
            }
        }
        // 如果设备状态变为speaking或listening，但不是从idle变过来，说明对话继续
        else if ((current_state == kDeviceStateSpeaking || current_state == kDeviceStateListening) && 
                 !in_conversation) {
            // 标记为对话中
            in_conversation = true;
            
            // 停止随机表情动画
            if (board->emoji_controller_) {
                board->ApplyIdleAnimation(false);
                board->emoji_controller_->ClearAnimationQueue();
                ESP_LOGI(TAG, "对话继续，停止随机表情动画");
            }
        }
        
        // 如果设备状态从speaking变为idle，说明AI刚刚回复完毕
        if (last_state == kDeviceStateSpeaking && current_state == kDeviceStateIdle) {
            // 记录AI回复结束的时间
            last_speak_end_time = xTaskGetTickCount();
            
            // 获取最近的AI回复内容
            const std::string& ai_response = board->last_ai_response_;
            
            // 如果有AI回复内容，则基于内容分析情感
            if (!ai_response.empty()) {
                ESP_LOGI(TAG, "AI回复结束，基于内容分析情感: %s", ai_response.c_str());
                
                // 直接处理AI回复内容，触发相应的表情和动作
                emotion_controller->ProcessAIResponse(ai_response);
            } else {
                // 如果没有AI回复内容，则使用随机情感
                static const char* emotions[] = {
                    "happy", "sad", "surprise", "confused", "neutral", "look_left", "look_right"
                };
                int random_index = rand() % (sizeof(emotions) / sizeof(emotions[0]));
                emotion_controller->TriggerEmotion(emotions[random_index]);
                
                ESP_LOGI(TAG, "AI回复结束，无内容，使用随机情感: %s", emotions[random_index]);
            }
        }
        
        // 如果设备状态从listening变为idle，说明用户刚刚输入完毕
        if (last_state == kDeviceStateListening && current_state == kDeviceStateIdle) {
            // 尝试处理用户输入的表情动作命令
            // 注意：这里我们不能直接获取用户输入内容，因为框架没有提供这个接口
            // 但我们可以在AI回复前检查常见的表情动作命令
            
            // 模拟处理几个常见的表情动作命令
            static const std::vector<std::string> common_commands = {
                "向左看", "向右看", "看左边", "看右边", "左看", "右看",
                "开心", "笑一笑", "高兴", "笑", "微笑",
                "悲伤", "伤心", "难过", "哭",
                "惊讶", "吃惊", "惊喜",
                "困惑", "疑惑", "迷惑",
                "正常", "平静", "中性", "恢复"
            };
            
            for (const auto& cmd : common_commands) {
                if (emotion_controller->ProcessEmotionCommand(cmd)) {
                    ESP_LOGI(TAG, "用户输入结束，尝试处理常见表情动作命令: %s", cmd.c_str());
                    break;
                }
            }
        }
        
        // 如果设备状态从listening或speaking变为idle，认为对话可能结束
        if ((last_state == kDeviceStateListening || last_state == kDeviceStateSpeaking) && 
            current_state == kDeviceStateIdle) {
            TickType_t current_time = xTaskGetTickCount();
            
            // 如果已经过了3秒，认为对话结束
            if (in_conversation && 
                ((current_time - last_speak_end_time) > pdMS_TO_TICKS(3000))) {
                in_conversation = false;
                
                // 恢复随机表情动画
                if (board->emoji_controller_) {
                    board->ApplyIdleAnimation(true);
                    ESP_LOGI(TAG, "对话结束，随机表情动画交由总开关决定");
                }
                
                // 触发中性情感
                emotion_controller->TriggerEmotion("neutral");
                ESP_LOGI(TAG, "对话结束，恢复中性情感");
            }
        }
        
        // 番茄钟画面的让屏/恢复。每 tick 都要跑，不能只挂在状态转换分支里
        board->SyncPomodoroScreen(current_state);

        // 更新上一次的设备状态
        last_state = current_state;

        // 延时100毫秒
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    
    // 注意：这里永远不会执行到，因为上面的循环是无限的
    delete params;
    vTaskDelete(NULL);
}

// 声明一个静态函数，用于处理AI回复
static void ProcessAIResponseTask(void* arg) {
    char* message = (char*)arg;
    if (message) {
        // 获取情感响应控制器
        EmotionResponseController* emotion_controller = nullptr;
        
        // 直接从EmojiBoard获取
        if (g_board_instance) {
            // 由于ProcessAIResponseTask是EmojiBoard的友元函数，可以直接访问私有成员
            emotion_controller = g_board_instance->emotion_controller_;
            ESP_LOGI("AIResponseTask", "从EmojiBoard获取情感控制器");
        }
        
        if (emotion_controller) {
            ESP_LOGI("AIResponseTask", "处理AI回复: %s", message);
            emotion_controller->ProcessAIResponse(message);
        } else {
            ESP_LOGW("AIResponseTask", "无法获取情感控制器，无法处理AI回复");
        }
    }
    
    // 释放消息内存
    if (message) {
        free(message);
    }
    
    // 删除任务
    vTaskDelete(NULL);
}

// 实现EmojiDisplay::SetEmotion方法
void EmojiDisplay::SetEmotion(const char* emotion) {
    ESP_LOGI(TAG, "小智AI框架识别到表情: %s", emotion);
    
    // 调用父类的SetEmotion方法，保持原有功能
    OledDisplay::SetEmotion(emotion);
    
    // 防止递归调用
    if (processing_ai_response_) {
        return;
    }

    // 番茄钟画面在前台时先让屏，接下来的表情动画才看得见
    // （见 YieldPomodoroScreenForAlert 的注释）
    if (board_) {
        board_->YieldPomodoroScreenForAlert(emotion);
    }

    // 将小智AI框架的表情映射到我们的表情动作
    if (board_ && board_->emotion_controller_) {
        std::string emotion_str(emotion);
        
        // 映射小智AI框架的表情到我们的表情动作
        std::string mapped_emotion;
        
        // 开心系列表情
        if (emotion_str == "happy") {
            mapped_emotion = "happy";
        } else if (emotion_str == "laughing") {
            mapped_emotion = "laughing";
        } else if (emotion_str == "funny") {
            mapped_emotion = "funny";
        } 
        // 悲伤系列表情
        else if (emotion_str == "sad") {
            mapped_emotion = "sad";
        } else if (emotion_str == "crying") {
            mapped_emotion = "cry";
        } 
        // 生气表情
        else if (emotion_str == "angry") {
            mapped_emotion = "anger";
        } 
        // 惊讶系列表情
        else if (emotion_str == "surprised") {
            mapped_emotion = "surprise";
        } else if (emotion_str == "shocked") {
            mapped_emotion = "shocked";
        } 
        // 困惑和思考表情
        else if (emotion_str == "confused") {
            mapped_emotion = "confused";
        } else if (emotion_str == "thinking") {
            mapped_emotion = "thinking";
        } 
        // 尴尬表情
        else if (emotion_str == "embarrassed") {
            mapped_emotion = "awkward";
        } 
        // 睡觉表情
        else if (emotion_str == "sleepy") {
            mapped_emotion = "sleep";
        } 
        // 眨眼表情
        else if (emotion_str == "winking") {
            mapped_emotion = "blink";
        } 
        // 酷酷的表情
        else if (emotion_str == "cool") {
            mapped_emotion = "cool";
        } 
        // 自信表情
        else if (emotion_str == "confident") {
            mapped_emotion = "confident";
        } 
        // 放松表情
        else if (emotion_str == "relaxed") {
            mapped_emotion = "relaxed";
        } 
        // 爱心和亲吻表情
        else if (emotion_str == "loving") {
            mapped_emotion = "loving";
        } else if (emotion_str == "kissy") {
            mapped_emotion = "kissy";
        } 
        // 美味表情
        else if (emotion_str == "delicious") {
            mapped_emotion = "delicious";
        } 
        // 奇怪表情
        else if (emotion_str == "silly") {
            mapped_emotion = "silly";
        } 
        // 中性表情
        else if (emotion_str == "neutral") {
            mapped_emotion = "neutral";
        } 
        // 对于其他表情，使用默认的中性表情
        else {
            mapped_emotion = "neutral";
            ESP_LOGW(TAG, "未识别的表情类型: %s，使用默认的中性表情", emotion_str.c_str());
        }
        
        ESP_LOGI(TAG, "映射到我们的表情动作: %s", mapped_emotion.c_str());
        
        // 创建一个单独的任务来执行表情动作，避免阻塞主线程
        // 复制表情字符串，因为它将在任务中使用
        char* emotion_copy = strdup(mapped_emotion.c_str());
        if (emotion_copy) {
            xTaskCreate([](void* arg) {
                char* emotion = (char*)arg;
                // 获取EmojiBoard实例
                if (g_board_instance && g_board_instance->emotion_controller_) {
                    // 使用TriggerEmotion方法触发表情动作，这是一个公有方法
                    g_board_instance->emotion_controller_->TriggerEmotion(emotion);
                }
                // 释放复制的字符串
                free(emotion);
                vTaskDelete(NULL);
            }, "emotion_task", 4096, emotion_copy, 1, NULL);
        }
    }
}

DECLARE_BOARD(EmojiBoard);
