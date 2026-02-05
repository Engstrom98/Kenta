#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"

static const char *TAG = "kenta";

// WiFi — set these to your network
#define WIFI_SSID     CONFIG_WIFI_SSID
#define WIFI_PASS     CONFIG_WIFI_PASS

// Server
#define SERVER_IP     CONFIG_SERVER_IP
#define SERVER_PORT   12345

// I2S pins (verified with GPIO toggle test)
#define PIN_SCK  32
#define PIN_WS   25
#define PIN_SD   33

// Push-to-talk button (BOOT button, active low)
#define PIN_BTN  0

// Audio config matching server expectations
#define SAMPLE_RATE   16000
#define SAMPLE_BITS   I2S_DATA_BIT_WIDTH_32BIT
#define DMA_BUF_COUNT 4
#define DMA_BUF_LEN   256

// End marker expected by server
static const uint8_t END_MARKER[4] = {0xDE, 0xAD, 0xBE, 0xEF};

static i2s_chan_handle_t rx_chan;
static SemaphoreHandle_t wifi_ready;

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xSemaphoreGive(wifi_ready);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ---------------------------------------------------------------------------
// I2S
// ---------------------------------------------------------------------------
static void i2s_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = DMA_BUF_COUNT;
    chan_cfg.dma_frame_num = DMA_BUF_LEN;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_chan));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(SAMPLE_BITS, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_SCK,
            .ws = PIN_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_SD,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_chan, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_chan));
    ESP_LOGI(TAG, "I2S initialized");
}

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------
static void button_init(void)
{
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << PIN_BTN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn_cfg));
}

static bool button_pressed(void)
{
    return gpio_get_level(PIN_BTN) == 0;  // BOOT button is active low
}

// ---------------------------------------------------------------------------
// TCP send
// ---------------------------------------------------------------------------
static int tcp_connect(void)
{
    struct sockaddr_in dest = {
        .sin_family = AF_INET,
        .sin_port = htons(SERVER_PORT),
    };
    inet_pton(AF_INET, SERVER_IP, &dest.sin_addr);

    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        ESP_LOGE(TAG, "Socket creation failed");
        return -1;
    }

    if (connect(sock, (struct sockaddr *)&dest, sizeof(dest)) != 0) {
        ESP_LOGE(TAG, "TCP connect to %s:%d failed", SERVER_IP, SERVER_PORT);
        close(sock);
        return -1;
    }

    ESP_LOGI(TAG, "Connected to server %s:%d", SERVER_IP, SERVER_PORT);
    return sock;
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------
void app_main(void)
{
    wifi_ready = xSemaphoreCreateBinary();

    wifi_init();
    i2s_init();
    button_init();

    // Wait for WiFi
    ESP_LOGI(TAG, "Waiting for WiFi...");
    xSemaphoreTake(wifi_ready, portMAX_DELAY);

    // Discard startup samples (datasheet p.10: zero output for 2^18 SCK cycles)
    static int32_t i2s_buf[256];
    static int16_t pcm_buf[256];
    size_t bytes_read;
    for (int i = 0; i < 8; i++) {
        i2s_channel_read(rx_chan, i2s_buf, sizeof(i2s_buf), &bytes_read, portMAX_DELAY);
    }

    ESP_LOGI(TAG, "Ready! Hold BOOT button to record, release to send.");

    while (1) {
        // Wait for button press
        while (!button_pressed()) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        ESP_LOGI(TAG, "Recording...");

        // Connect to server
        int sock = tcp_connect();
        if (sock < 0) {
            // Wait for button release before retrying
            while (button_pressed()) vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        // Stream audio while button is held
        while (button_pressed()) {
            esp_err_t ret = i2s_channel_read(rx_chan, i2s_buf, sizeof(i2s_buf), &bytes_read, portMAX_DELAY);
            if (ret != ESP_OK) continue;

            int num_samples = bytes_read / sizeof(int32_t);

            // Convert 32-bit I2S data (24-bit in upper bits) to 16-bit PCM
            for (int i = 0; i < num_samples; i++) {
                pcm_buf[i] = (int16_t)(i2s_buf[i] >> 16);
            }

            send(sock, pcm_buf, num_samples * sizeof(int16_t), 0);
        }

        // Send end marker and close
        send(sock, END_MARKER, sizeof(END_MARKER), 0);
        close(sock);
        ESP_LOGI(TAG, "Sent. Waiting for next press...");

        vTaskDelay(pdMS_TO_TICKS(200));  // debounce
    }
}
