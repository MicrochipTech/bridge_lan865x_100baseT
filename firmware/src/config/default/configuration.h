/*******************************************************************************
  System Configuration Header

  File Name:
    configuration.h

  Summary:
    Build-time configuration header for the system defined by this project.

  Description:
    An MPLAB Project may have multiple configurations.  This file defines the
    build-time options for a single configuration.

  Remarks:
    This configuration header must not define any prototypes or data
    definitions (or include any files that do).  It only provides macro
    definitions for build-time configuration options

*******************************************************************************/

// DOM-IGNORE-BEGIN
/*******************************************************************************
* Copyright (C) 2018 Microchip Technology Inc. and its subsidiaries.
*
* Subject to your compliance with these terms, you may use Microchip software
* and any derivatives exclusively with Microchip products. It is your
* responsibility to comply with third party license terms applicable to your
* use of third party software (including open source software) that may
* accompany Microchip software.
*
* THIS SOFTWARE IS SUPPLIED BY MICROCHIP "AS IS". NO WARRANTIES, WHETHER
* EXPRESS, IMPLIED OR STATUTORY, APPLY TO THIS SOFTWARE, INCLUDING ANY IMPLIED
* WARRANTIES OF NON-INFRINGEMENT, MERCHANTABILITY, AND FITNESS FOR A
* PARTICULAR PURPOSE.
*
* IN NO EVENT WILL MICROCHIP BE LIABLE FOR ANY INDIRECT, SPECIAL, PUNITIVE,
* INCIDENTAL OR CONSEQUENTIAL LOSS, DAMAGE, COST OR EXPENSE OF ANY KIND
* WHATSOEVER RELATED TO THE SOFTWARE, HOWEVER CAUSED, EVEN IF MICROCHIP HAS
* BEEN ADVISED OF THE POSSIBILITY OR THE DAMAGES ARE FORESEEABLE. TO THE
* FULLEST EXTENT ALLOWED BY LAW, MICROCHIP'S TOTAL LIABILITY ON ALL CLAIMS IN
* ANY WAY RELATED TO THIS SOFTWARE WILL NOT EXCEED THE AMOUNT OF FEES, IF ANY,
* THAT YOU HAVE PAID DIRECTLY TO MICROCHIP FOR THIS SOFTWARE.
*******************************************************************************/
// DOM-IGNORE-END

#ifndef CONFIGURATION_H
#define CONFIGURATION_H

// *****************************************************************************
// *****************************************************************************
// Section: Included Files
// *****************************************************************************
// *****************************************************************************
/*  This section Includes other configuration headers necessary to completely
    define this configuration.
*/

#include "user.h"
#include "device.h"

// DOM-IGNORE-BEGIN
#ifdef __cplusplus  // Provide C++ Compatibility

extern "C" {

#endif
// DOM-IGNORE-END

// *****************************************************************************
// *****************************************************************************
// Section: System Configuration
// *****************************************************************************
// *****************************************************************************



// *****************************************************************************
// *****************************************************************************
// Section: System Service Configuration
// *****************************************************************************
// *****************************************************************************
/* TIME System Service Configuration Options */
#define SYS_TIME_INDEX_0                            (0)
#define SYS_TIME_MAX_TIMERS                         (5)
#define SYS_TIME_HW_COUNTER_WIDTH                   (16)
#define SYS_TIME_HW_COUNTER_PERIOD                  (0xFFFFU)
#define SYS_TIME_HW_COUNTER_HALF_PERIOD             (SYS_TIME_HW_COUNTER_PERIOD>>1)
#define SYS_TIME_CPU_CLOCK_FREQUENCY                (120000000)
#define SYS_TIME_COMPARE_UPDATE_EXECUTION_CYCLES    (232)

#define SYS_CONSOLE_INDEX_0                       0





#define SYS_CMD_ENABLE
#define SYS_CMD_DEVICE_MAX_INSTANCES       SYS_CONSOLE_DEVICE_MAX_INSTANCES
#define SYS_CMD_PRINT_BUFFER_SIZE          1024U
#define SYS_CMD_BUFFER_DMA_READY



#define SYS_DEBUG_ENABLE
#define SYS_DEBUG_GLOBAL_ERROR_LEVEL       SYS_ERROR_DEBUG
#define SYS_DEBUG_BUFFER_DMA_READY
#define SYS_DEBUG_USE_CONSOLE


#define SYS_CONSOLE_DEVICE_MAX_INSTANCES   			(1U)
#define SYS_CONSOLE_UART_MAX_INSTANCES 	   			(1U)
#define SYS_CONSOLE_USB_CDC_MAX_INSTANCES 	   		(0U)
#define SYS_CONSOLE_PRINT_BUFFER_SIZE        		(200U)




// *****************************************************************************
// *****************************************************************************
// Section: Driver Configuration
// *****************************************************************************
// *****************************************************************************

/*** LAN865X Driver Configuration ***/
/*** Driver Compilation and static configuration options. ***/
#define TCPIP_IF_LAN865X

#define DRV_LAN865X_INSTANCES_NUMBER         1

#define DRV_LAN865X_SPI_DRIVER_INSTANCE_IDX0 0
#define DRV_LAN865X_CLIENT_INSTANCES_IDX0    1
#define DRV_LAN865X_SPI_FREQ_IDX0            15000000
#define DRV_LAN865X_MAC_RX_DESCRIPTORS_IDX0  2
#define DRV_LAN865X_MAX_RX_BUFFER_IDX0       1536
#define DRV_LAN865X_SPI_CS_IDX0              SYS_PORT_PIN_PC15
#define DRV_LAN865X_INTERRUPT_PIN_IDX0       SYS_PORT_PIN_PC14
#define DRV_LAN865X_RESET_PIN_IDX0           SYS_PORT_PIN_PC18
#define DRV_LAN865X_PROMISCUOUS_IDX0         true
#define DRV_LAN865X_TX_CUT_THROUGH_IDX0      true
#define DRV_LAN865X_RX_CUT_THROUGH_IDX0      false
#define DRV_LAN865X_CHUNK_SIZE_IDX0          64
#define DRV_LAN865X_CHUNK_XACT_IDX0          31
#define DRV_LAN865X_PLCA_ENABLE_IDX0         true
#define DRV_LAN865X_PLCA_NODE_ID_IDX0        5
#define DRV_LAN865X_PLCA_NODE_COUNT_IDX0     8
#define DRV_LAN865X_PLCA_BURST_COUNT_IDX0    0
#define DRV_LAN865X_PLCA_BURST_TIMER_IDX0    128



/* SPI Driver Common Configuration Options */
#define DRV_SPI_INSTANCES_NUMBER              (1U)

/* SPI Driver Instance 0 Configuration Options */
#define DRV_SPI_INDEX_0                       0
#define DRV_SPI_CLIENTS_NUMBER_IDX0           1
#define DRV_SPI_DMA_MODE
#define DRV_SPI_XMIT_DMA_CH_IDX0              SYS_DMA_CHANNEL_0
#define DRV_SPI_RCV_DMA_CH_IDX0               SYS_DMA_CHANNEL_1
#define DRV_SPI_QUEUE_SIZE_IDX0               1

/*** MIIM Driver Configuration ***/
#define DRV_MIIM_ETH_MODULE_ID_0                GMAC_BASE_ADDRESS
#define DRV_MIIM_DRIVER_INDEX_0                 0
#define DRV_MIIM_INSTANCES_NUMBER           1
#define DRV_MIIM_INSTANCE_OPERATIONS        4
#define DRV_MIIM_INSTANCE_CLIENTS           2
#define DRV_MIIM_CLIENT_OP_PROTECTION   false
#define DRV_MIIM_COMMANDS   false
#define DRV_MIIM_DRIVER_OBJECT              DRV_MIIM_OBJECT_BASE_Default            



/* Emulated EEPROM library instance 0 Configuration Options */
#define EMULATED_EEPROM0                       0



// *****************************************************************************
// *****************************************************************************
// Section: Middleware & Other Library Configuration
// *****************************************************************************
// *****************************************************************************


/*** DNS Client Configuration ***/
#define TCPIP_STACK_USE_DNS
#define TCPIP_DNS_CLIENT_SERVER_TMO					60
#define TCPIP_DNS_CLIENT_TASK_PROCESS_RATE			200
#define TCPIP_DNS_CLIENT_CACHE_ENTRIES				5
#define TCPIP_DNS_CLIENT_CACHE_ENTRY_TMO			0
#define TCPIP_DNS_CLIENT_CACHE_PER_IPV4_ADDRESS		5
#define TCPIP_DNS_CLIENT_CACHE_PER_IPV6_ADDRESS		1
#define TCPIP_DNS_CLIENT_ADDRESS_TYPE			    IP_ADDRESS_TYPE_IPV4
#define TCPIP_DNS_CLIENT_CACHE_DEFAULT_TTL_VAL		1200
#define TCPIP_DNS_CLIENT_LOOKUP_RETRY_TMO			2
#define TCPIP_DNS_CLIENT_MAX_HOSTNAME_LEN			64
#define TCPIP_DNS_CLIENT_MAX_SELECT_INTERFACES		4
#define TCPIP_DNS_CLIENT_DELETE_OLD_ENTRIES			true
#define TCPIP_DNS_CLIENT_CONSOLE_CMD               	true
#define TCPIP_DNS_CLIENT_USER_NOTIFICATION   false



/*** ICMPv4 Server Configuration ***/
#define TCPIP_STACK_USE_ICMP_SERVER
#define TCPIP_ICMP_ECHO_ALLOW_BROADCASTS    false

/*** ICMPv4 Client Configuration ***/
#define TCPIP_STACK_USE_ICMP_CLIENT
#define TCPIP_ICMP_ECHO_REQUEST_TIMEOUT        500
#define TCPIP_ICMP_TASK_TICK_RATE              33
#define TCPIP_STACK_MAX_CLIENT_ECHO_REQUESTS   4
#define TCPIP_ICMP_COMMAND_ENABLE              true
#define TCPIP_STACK_COMMANDS_ICMP_ECHO_REQUESTS         4
#define TCPIP_STACK_COMMANDS_ICMP_ECHO_REQUEST_DELAY    1000
#define TCPIP_STACK_COMMANDS_ICMP_ECHO_TIMEOUT          5000
#define TCPIP_STACK_COMMANDS_ICMP_ECHO_REQUEST_BUFF_SIZE    2000
#define TCPIP_STACK_COMMANDS_ICMP_ECHO_REQUEST_DATA_SIZE    100


/*** TCP Configuration ***/
#define TCPIP_TCP_MAX_SEG_SIZE_TX		        	1460
#define TCPIP_TCP_SOCKET_DEFAULT_TX_SIZE			512
#define TCPIP_TCP_SOCKET_DEFAULT_RX_SIZE			512
#define TCPIP_TCP_DYNAMIC_OPTIONS             			true
#define TCPIP_TCP_START_TIMEOUT_VAL		        	1000
#define TCPIP_TCP_DELAYED_ACK_TIMEOUT		    		100
#define TCPIP_TCP_FIN_WAIT_2_TIMEOUT		    		5000
#define TCPIP_TCP_KEEP_ALIVE_TIMEOUT		    		10000
#define TCPIP_TCP_CLOSE_WAIT_TIMEOUT		    		0
#define TCPIP_TCP_MAX_RETRIES		            		5
#define TCPIP_TCP_MAX_UNACKED_KEEP_ALIVES			6
#define TCPIP_TCP_MAX_SYN_RETRIES		        	3
#define TCPIP_TCP_AUTO_TRANSMIT_TIMEOUT_VAL			40
#define TCPIP_TCP_WINDOW_UPDATE_TIMEOUT_VAL			200
#define TCPIP_TCP_MAX_SOCKETS		                10
#define TCPIP_TCP_TASK_TICK_RATE		        	5
#define TCPIP_TCP_MSL_TIMEOUT		        	    0
#define TCPIP_TCP_QUIET_TIME		        	    0
#define TCPIP_TCP_COMMANDS   true
#define TCPIP_TCP_EXTERN_PACKET_PROCESS   true
#define TCPIP_TCP_DISABLE_CRYPTO_USAGE		        	    false



#define TCPIP_STACK_USE_ZEROCONF_LINK_LOCAL
#define TCPIP_ZC_LL_PROBE_WAIT 1
#define TCPIP_ZC_LL_PROBE_MIN 1
#define TCPIP_ZC_LL_PROBE_MAX 2
#define TCPIP_ZC_LL_PROBE_NUM 3
#define TCPIP_ZC_LL_ANNOUNCE_WAIT 2
#define TCPIP_ZC_LL_ANNOUNCE_NUM 2
#define TCPIP_ZC_LL_ANNOUNCE_INTERVAL 2
#define TCPIP_ZC_LL_MAX_CONFLICTS 10
#define TCPIP_ZC_LL_RATE_LIMIT_INTERVAL 60
#define TCPIP_ZC_LL_DEFEND_INTERVAL 10
#define TCPIP_ZC_LL_IPV4_LLBASE 0xa9fe0100
#define TCPIP_ZC_LL_IPV4_LLBASE_MASK 0xffff
#define TCPIP_ZC_LL_TASK_TICK_RATE 113
/* HAND-PATCH to MCC-generated code (CLAUDE.md section 3): mDNS-SD dropped
   (2026-09-06) - its own decompression parser asserts on essentially every
   incoming query (confirmed live, not just deep/unusual compression chains -
   a trivial hand-crafted uncompressed query triggered it too), and it never
   answers a directed query as a result; only its own boot-time self-announce
   ever reaches the wire. Board discovery is being replaced with a small
   custom plaintext UDP broadcast protocol instead - see discover.py/app.c.
   Undefining this alone drops the whole zero_conf_multicast_dns.c body (see
   its own `#if defined(TCPIP_STACK_USE_ZEROCONF_LINK_LOCAL) &&
   defined(TCPIP_STACK_USE_ZEROCONF_MDNS_SD)` guard) so nothing is left for
   the linker to pull in. This is a stopgap: the real removal is unchecking
   "Use Multicast DNS Zero Config (Bonjour)" in MCC and regenerating - do
   that instead of re-adding this define back.
#define TCPIP_STACK_USE_ZEROCONF_MDNS_SD */
#define TCPIP_ZC_MDNS_TASK_TICK_RATE 63
#define TCPIP_ZC_MDNS_PORT 5353
#define TCPIP_ZC_MDNS_MAX_HOST_NAME_SIZE 32
#define TCPIP_ZC_MDNS_MAX_LABEL_SIZE 64
#define TCPIP_ZC_MDNS_MAX_RR_NAME_SIZE 256
#define TCPIP_ZC_MDNS_MAX_SRV_TYPE_SIZE 32
#define TCPIP_ZC_MDNS_MAX_SRV_NAME_SIZE 64
#define TCPIP_ZC_MDNS_MAX_TXT_DATA_SIZE 128
#define TCPIP_ZC_MDNS_RESOURCE_RECORD_TTL_VAL 3600
#define TCPIP_ZC_MDNS_MAX_RR_NUM 4
#define TCPIP_ZC_MDNS_PROBE_WAIT 750
#define TCPIP_ZC_MDNS_PROBE_INTERVAL 250
#define TCPIP_ZC_MDNS_PROBE_NUM 3
#define TCPIP_ZC_MDNS_MAX_PROBE_CONFLICT_NUM 30
#define TCPIP_ZC_MDNS_ANNOUNCE_NUM 3
#define TCPIP_ZC_MDNS_ANNOUNCE_INTERVAL 250
#define TCPIP_ZC_MDNS_ANNOUNCE_WAIT 250



/*** ARP Configuration ***/
#define TCPIP_ARP_CACHE_ENTRIES                 		5
#define TCPIP_ARP_CACHE_DELETE_OLD		        	true
#define TCPIP_ARP_CACHE_SOLVED_ENTRY_TMO			1200
#define TCPIP_ARP_CACHE_PENDING_ENTRY_TMO			60
#define TCPIP_ARP_CACHE_PENDING_RETRY_TMO			2
#define TCPIP_ARP_CACHE_PERMANENT_QUOTA		    		50
#define TCPIP_ARP_CACHE_PURGE_THRESHOLD		    		75
#define TCPIP_ARP_CACHE_PURGE_QUANTA		    		1
#define TCPIP_ARP_CACHE_ENTRY_RETRIES		    		3
#define TCPIP_ARP_GRATUITOUS_PROBE_COUNT			1
#define TCPIP_ARP_TASK_PROCESS_RATE		        	2000
#define TCPIP_ARP_PRIMARY_CACHE_ONLY		        	true
#define TCPIP_ARP_COMMANDS false



	/*** tcpip_cmd Configuration ***/
	#define TCPIP_STACK_COMMAND_ENABLE



/* Network Configuration Index 0 */
#define TCPIP_NETWORK_DEFAULT_INTERFACE_NAME_IDX0 "LAN865x"

#define TCPIP_NETWORK_DEFAULT_HOST_NAME_IDX0              "MCHP_LAN865x"
#define TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX0               "00:04:25:1C:A0:02"

#define TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX0         "192.168.0.11"
#define TCPIP_NETWORK_DEFAULT_IP_MASK_IDX0            "255.255.255.0"
#define TCPIP_NETWORK_DEFAULT_GATEWAY_IDX0            "192.168.0.1"
#define TCPIP_NETWORK_DEFAULT_DNS_IDX0                "192.168.0.1"
#define TCPIP_NETWORK_DEFAULT_SECOND_DNS_IDX0         "0.0.0.0"
#define TCPIP_NETWORK_DEFAULT_POWER_MODE_IDX0         "full"
#define TCPIP_NETWORK_DEFAULT_INTERFACE_FLAGS_IDX0            \
                                                    TCPIP_NETWORK_CONFIG_DHCP_CLIENT_ON |\
                                                    TCPIP_NETWORK_CONFIG_DNS_CLIENT_ON |\
                                                    TCPIP_NETWORK_CONFIG_IP_STATIC
                                                    
#define TCPIP_NETWORK_DEFAULT_MAC_DRIVER_IDX0         DRV_LAN865X_MACObject_0



#define TCPIP_NETWORK_VLAN_ID_IDX0         0
#define TCPIP_NETWORK_VLAN_PCP_IDX0         0



/* Network Configuration Index 1 */
#define TCPIP_NETWORK_DEFAULT_INTERFACE_NAME_IDX1 "GMAC"
#define TCPIP_IF_GMAC  

#define TCPIP_NETWORK_DEFAULT_HOST_NAME_IDX1              "MCHPBOARD_C"
#define TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX1               "00:04:25:1C:A0:03"

#define TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX1         "192.168.0.12"
#define TCPIP_NETWORK_DEFAULT_IP_MASK_IDX1            "255.255.255.0"
#define TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1            "192.168.0..1"
#define TCPIP_NETWORK_DEFAULT_DNS_IDX1                "192.168.0.1"
#define TCPIP_NETWORK_DEFAULT_SECOND_DNS_IDX1         "0.0.0.0"
#define TCPIP_NETWORK_DEFAULT_POWER_MODE_IDX1         "full"
#define TCPIP_NETWORK_DEFAULT_INTERFACE_FLAGS_IDX1            \
                                                    TCPIP_NETWORK_CONFIG_DHCP_CLIENT_ON |\
                                                    TCPIP_NETWORK_CONFIG_DNS_CLIENT_ON |\
                                                    TCPIP_NETWORK_CONFIG_IP_STATIC
                                                    
#define TCPIP_NETWORK_DEFAULT_MAC_DRIVER_IDX1         DRV_GMAC_Object



#define TCPIP_NETWORK_VLAN_ID_IDX1         0
#define TCPIP_NETWORK_VLAN_PCP_IDX1         0



/*** telnet Configuration ***/
#define TCPIP_STACK_USE_TELNET_SERVER
/* 1, not the MCC-generated 2: capped to the one TLS session this board's RAM
   budget was scoped for (session log) once Telnet went TLS-only below. */
#define TCPIP_TELNET_MAX_CONNECTIONS    1
#define TCPIP_TELNET_TASK_TICK_RATE     100
#define TCPIP_TELNET_SKT_TX_BUFF_SIZE   3200
#define TCPIP_TELNET_SKT_RX_BUFF_SIZE   0
#define TCPIP_TELNET_LISTEN_PORT        23
#define TCPIP_TELNET_PRINT_BUFF_SIZE    200
#define TCPIP_TELNET_LINE_BUFF_SIZE     80
#define TCPIP_TELNET_USERNAME_SIZE      15
#define TCPIP_TELNET_CONFIG_FLAGS       \
                                       TCPIP_TELNET_FLAG_NONE

#define TCPIP_TELNET_OBSOLETE_AUTHENTICATION false
#define TCPIP_TELNET_AUTHENTICATION_CONN_INFO true



/*** iperf Configuration ***/
#define TCPIP_STACK_USE_IPERF
#define TCPIP_IPERF_TX_BUFFER_SIZE		4096
#define TCPIP_IPERF_RX_BUFFER_SIZE  	4096
#define TCPIP_IPERF_TX_WAIT_TMO     	100
#define TCPIP_IPERF_TX_QUEUE_LIMIT  	2
#define TCPIP_IPERF_TIMING_ERROR_MARGIN 0
#define TCPIP_IPERF_MAX_INSTANCES       1
#define TCPIP_IPERF_TX_BW_LIMIT  		10



/*** IPv4 Configuration ***/
#define TCPIP_IPV4_ARP_SLOTS                        10
#define TCPIP_IPV4_EXTERN_PACKET_PROCESS   false

#define TCPIP_IPV4_COMMANDS false

#define TCPIP_IPV4_FORWARDING_ENABLE    false 





/*** TCPIP Heap Configuration ***/
#define TCPIP_STACK_USE_INTERNAL_HEAP
#define TCPIP_STACK_DRAM_SIZE                       98304
#define TCPIP_STACK_DRAM_RUN_LIMIT                  2048

#define TCPIP_STACK_MALLOC_FUNC                     malloc

#define TCPIP_STACK_CALLOC_FUNC                     calloc

#define TCPIP_STACK_FREE_FUNC                       free



#define TCPIP_STACK_HEAP_USE_FLAGS                   TCPIP_STACK_HEAP_FLAG_ALLOC_UNCACHED

#define TCPIP_STACK_HEAP_USAGE_CONFIG                TCPIP_STACK_HEAP_USE_DEFAULT

#define TCPIP_STACK_SUPPORTED_HEAPS                  1




// *****************************************************************************
// *****************************************************************************
// Section: TCPIP Stack Configuration
// *****************************************************************************
// *****************************************************************************

#define TCPIP_STACK_USE_IPV4
#define TCPIP_STACK_USE_TCP
#define TCPIP_STACK_USE_UDP

#define TCPIP_STACK_TICK_RATE		        		1
#define TCPIP_STACK_SECURE_PORT_ENTRIES             10
#define TCPIP_STACK_LINK_RATE		        		333

#define TCPIP_STACK_ALIAS_INTERFACE_SUPPORT   true

#define TCPIP_STACK_VLAN_INTERFACE_SUPPORT   false

#define TCPIP_PACKET_LOG_ENABLE     0

/* TCP/IP stack event notification */
#define TCPIP_STACK_USE_EVENT_NOTIFICATION
#define TCPIP_STACK_USER_NOTIFICATION   false
#define TCPIP_STACK_DOWN_OPERATION   true
#define TCPIP_STACK_IF_UP_DOWN_OPERATION   true
#define TCPIP_STACK_MAC_DOWN_OPERATION  true
#define TCPIP_STACK_INTERFACE_CHANGE_SIGNALING   false
#define TCPIP_STACK_CONFIGURATION_SAVE_RESTORE   true
#define TCPIP_STACK_EXTERN_PACKET_PROCESS   true
#define TCPIP_STACK_RUN_TIME_INIT   false

#define TCPIP_STACK_INTMAC_COUNT           1





/*** GMAC Configuration ***/
#define DRV_GMAC
#define DRV_SAME5x
#define TCPIP_GMAC_TX_DESCRIPTORS_COUNT_DUMMY    1
#define TCPIP_GMAC_RX_DESCRIPTORS_COUNT_DUMMY    1
#define TCPIP_GMAC_RX_BUFF_SIZE_DUMMY            64
#define TCPIP_GMAC_TX_BUFF_SIZE_DUMMY            64
#define TCPIP_GMAC_QUEUE_0                                  true  
/*** QUEUE 0 TX Configuration ***/
#define TCPIP_GMAC_TX_DESCRIPTORS_COUNT_QUE0            8
#define TCPIP_GMAC_MAX_TX_PKT_SIZE_QUE0                 1536
/*** QUEUE 0 RX Configuration ***/
#define TCPIP_GMAC_RX_DESCRIPTORS_COUNT_QUE0            8
#define TCPIP_GMAC_RX_BUFF_SIZE_QUE0                    1536
#define TCPIP_GMAC_RX_DEDICATED_BUFFERS_QUE0            8
#define TCPIP_GMAC_RX_ADDL_BUFF_COUNT_QUE0              2
#define TCPIP_GMAC_RX_BUFF_COUNT_THRESHOLD_QUE0         1
#define TCPIP_GMAC_RX_BUFF_ALLOC_COUNT_QUE0             2
#define TCPIP_GMAC_RX_FILTERS                       \
                                                        TCPIP_MAC_RX_FILTER_TYPE_BCAST_ACCEPT |\
                                                        TCPIP_MAC_RX_FILTER_TYPE_MCAST_ACCEPT |\
                                                        TCPIP_MAC_RX_FILTER_TYPE_UCAST_ACCEPT |\
                                                        TCPIP_MAC_RX_FILTER_TYPE_CRC_ERROR_REJECT |\
                                                        TCPIP_MAC_RX_FILTER_TYPE_ALL_ACCEPT |\
                                                          0
       
#define TCPIP_GMAC_SCREEN1_COUNT_QUE        0 
#define TCPIP_GMAC_SCREEN2_COUNT_QUE        0  

#define TCPIP_GMAC_ETH_OPEN_FLAGS                   \
                                                        TCPIP_ETH_OPEN_AUTO |\
                                                        TCPIP_ETH_OPEN_FDUPLEX |\
                                                        TCPIP_ETH_OPEN_HDUPLEX |\
                                                        TCPIP_ETH_OPEN_100 |\
                                                        TCPIP_ETH_OPEN_10 |\
                                                        TCPIP_ETH_OPEN_MDIX_AUTO |\
                                                            TCPIP_ETH_OPEN_RMII |\
                                                        0

#define TCPIP_GMAC_MODULE_ID                       GMAC_BASE_ADDRESS

#define TCPIP_INTMAC_PERIPHERAL_CLK                 120000000

#define DRV_GMAC_RX_CHKSM_OFFLOAD               (TCPIP_MAC_CHECKSUM_NONE)           
#define DRV_GMAC_TX_CHKSM_OFFLOAD               (TCPIP_MAC_CHECKSUM_NONE)       
#define TCPIP_GMAC_TX_PRIO_COUNT                1
#define TCPIP_GMAC_RX_PRIO_COUNT                1
#define DRV_GMAC_NUMBER_OF_QUEUES               1
#define DRV_GMAC_RMII_MODE                      0


#define DRV_GMAC_MULTI_CLIENT        			false




/*** UDP Configuration ***/
#define TCPIP_UDP_MAX_SOCKETS		                	10
#define TCPIP_UDP_SOCKET_DEFAULT_TX_SIZE		    	512
#define TCPIP_UDP_SOCKET_DEFAULT_TX_QUEUE_LIMIT    	 	3
#define TCPIP_UDP_SOCKET_DEFAULT_RX_QUEUE_LIMIT			3
#define TCPIP_UDP_USE_POOL_BUFFERS   false
#define TCPIP_UDP_USE_TX_CHECKSUM             			true
#define TCPIP_UDP_USE_RX_CHECKSUM             			true
#define TCPIP_UDP_COMMANDS   false
#define TCPIP_UDP_EXTERN_PACKET_PROCESS   false



/*** wolfCrypt Library Configuration ***/
#define MICROCHIP_PIC32
#define MICROCHIP_MPLAB_HARMONY
#define MICROCHIP_MPLAB_HARMONY_3
#define HAVE_MCAPI
#define SIZEOF_LONG_LONG 8
#define WOLFSSL_USER_IO
#define NO_WRITEV
#define NO_FILESYSTEM
#define USE_FAST_MATH
#define NO_PWDBASED
#define HAVE_MCAPI
#define WOLF_CRYPTO_CB  // provide call-back support
/* WOLFCRYPT_ONLY removed for the eth1/Telnet+bootload TLS experiment (branch
   t1s-t1s-bridge-lan8670): pulls in the actual TLS protocol layer
   (ssl.c/internal.c/tls.c, vendored from the same net_10base_t1s-pinned
   wolfssl v5.4.0 package under third_party/wolfssl/wolfssl/src/), not just
   the crypto primitives that were already linked in unused. TLS 1.2-only
   (no tls13.c vendored, NO_OLD_TLS below forces >=1.2) to keep this to the
   single shared TLS session slot per role this board can actually afford -
   see the RAM/heap discussion in the session log.

   NO_WOLFSSL_CLIENT (server-only) REMOVED for the MQTT-over-TLS work
   (docs/mqtt-tls-agent-prompt.md): the board now also dials OUT as a TLS
   client (net_pres_enc_glue_client.c, registered as NET_PRES's stream-CLIENT
   provider) to reach the MQTT broker, in addition to the existing TLS
   *server* roles (Telnet, bootload, cert_provision). wolfSSL's client-role
   code (wolfSSL_connect(), wolfTLSv1_2_client_method(), etc.) was already
   vendored in ssl.c/internal.c behind '#ifndef NO_WOLFSSL_CLIENT' - removing
   this macro just re-enables it, no additional wolfSSL sources needed. */
#define NO_OLD_TLS
#define NO_SESSION_CACHE        /* one session at a time - resumption caching buys nothing here */
/* This board has no RTC and no NTP/SNTP client wired up - confirmed no
   _gettimeofday/_times syscall (libc_syscalls.c) and no active RTC peripheral
   (RTC_Handler in the link is only the unused weak startup-code stub). XC32's
   libc time() still resolves (it's in the link), but returns nothing close to
   the real date - every certificate's notBefore would look like it is still
   in the future relative to that, and wolfSSL would reject ANY certificate,
   including a freshly issued, otherwise-valid one, as "not yet valid".
   NO_ASN_TIME skips date validation entirely - the honest fix would be a real
   time source (RTC seeded from build time, or SNTP once the stack is up),
   out of scope for this experiment. */
#define NO_ASN_TIME
/* RSA-2048 sign/verify measured at ~1.6s (cpuload, live handshake) using
   USE_FAST_MATH's generic TFM bignum path - the whole single-threaded
   superloop blocks for that long. WOLFSSL_HAVE_SP_RSA switches just the RSA
   operations (not the whole math backend - USE_FAST_MATH/TFM stays for
   everything else) to wolfSSL's hand-written Cortex-M assembly bignum
   routines (sp_cortexm.c, already vendored, previously dead code with
   neither macro set); WOLFSSL_SP_ARM_CORTEX_M_ASM selects that ASM path
   specifically over the generic-C fallback inside the same file. */
#define WOLFSSL_HAVE_SP_RSA
#define WOLFSSL_SP_ARM_CORTEX_M_ASM
// ---------- FUNCTIONAL CONFIGURATION START ----------
#define WOLFSSL_AES_SMALL_TABLES
#define NO_MD4
#define WOLFSSL_SHA224
#define WOLFSSL_AES_128
#define WOLFSSL_AES_192
#define WOLFSSL_AES_256
#define WOLFSSL_AES_DIRECT
#define HAVE_AES_DECRYPT
#define HAVE_AES_ECB
#define HAVE_AES_CBC
#define WOLFSSL_AES_COUNTER
#define WOLFSSL_AES_OFB
#define HAVE_AESGCM
#define HAVE_AESCCM
#define NO_RC4
#define NO_HC128
#define NO_RABBIT
#define HAVE_ECC
/* Needed for wolfSSL to send the elliptic_curves/supported_groups extension
   in the ClientHello (tls.c, gated by this macro throughout) - without it,
   the existing Telnet/bootload/cert_provision SERVER role still worked
   (the PEER, a Python client, sends this extension and the server just
   picks a curve from it), but the new MQTT CLIENT role
   (net_pres_enc_glue_client.c) does not: confirmed live 2026-09-07 via a
   tshark capture of the ClientHello (only a signature_algorithms
   extension, no elliptic_curves) against a Python/OpenSSL 3.x broker,
   which failed the handshake with NO_SHARED_CIPHER - OpenSSL refuses to
   select any ECDHE_* suite (the only ones offered, since NO_DH removed the
   plain-DHE ones and static-RSA suites aren't in wolfSSL's default list
   either) when the client never declared which curve it supports. */
#define HAVE_TLS_EXTENSIONS
#define HAVE_SUPPORTED_CURVES
#define NO_DH
#define NO_DSA
#define FP_MAX_BITS 4096
#define USE_CERT_BUFFERS_2048
#define NO_DEV_RANDOM
#define HAVE_HASHDRBG
#define WC_NO_HARDEN
#define SINGLE_THREADED
#define NO_SIG_WRAPPER
#define NO_ERROR_STRINGS
#define WOLFSSL_MAX_ERROR_SZ 38 // Fix Mandatory Misra 21.18 caused by removing error strings with defining NO_ERROR_STRINGS
#define NO_WOLFSSL_MEMORY
// ---------- FUNCTIONAL CONFIGURATION END ----------

/* MPLAB Harmony Net Presentation Layer Definitions*/
#define NET_PRES_NUM_INSTANCE 1
#define NET_PRES_NUM_SOCKETS 10
#define NET_PRES_LOG_VERBOSE 0


	

#define TCPIP_STACK_NETWORK_INTERAFCE_COUNT  	2



/*** Bridge Configuration ***/
#define TCPIP_STACK_USE_MAC_BRIDGE
#define TCPIP_STACK_MAC_BRIDGE_COMMANDS true
#define TCPIP_MAC_BRIDGE_FDB_TABLE_ENTRIES          17
#define TCPIP_MAC_BRIDGE_MAX_PORTS_NO               2
#define TCPIP_MAC_BRIDGE_PACKET_POOL_SIZE           8
#define TCPIP_MAC_BRIDGE_PACKET_SIZE                1536
#define TCPIP_MAC_BRIDGE_PACKET_POOL_REPLENISH      2
#define TCPIP_MAC_BRIDGE_DCPT_POOL_SIZE             16
#define TCPIP_MAC_BRIDGE_DCPT_POOL_REPLENISH        4
/* Advanced */
#define TCPIP_MAC_BRIDGE_ENTRY_TIMEOUT              300
#define TCPIP_MAC_BRIDGE_MAX_TRANSIT_DELAY          1
#define TCPIP_MAC_BRIDGE_TASK_RATE                  333

#define TCPIP_MAC_BRIDGE_STATISTICS          		true
#define TCPIP_MAC_BRIDGE_EVENT_NOTIFY          		true

#define TCPIP_MAC_BRIDGE_IF_NAME_TABLE false

#define TCPIP_MC_BRIDGE_INIT_FLAGS                  \
                                                    0

#define TCPIP_STACK_MAC_BRIDGE_DISABLE_GLUE_PORTS false


#define DRV_LAN8742A_PHY_CONFIG_FLAGS       ( 0 \
                                                    | DRV_ETHPHY_CFG_RMII \
                                                    )
                                                    
#define DRV_LAN8742A_PHY_LINK_INIT_DELAY            500
#define DRV_LAN8742A_PHY_ADDRESS                    0
#define DRV_LAN8742A_PHY_PERIPHERAL_ID              GMAC_BASE_ADDRESS
#define DRV_ETHPHY_LAN8742A_NEG_INIT_TMO            1
#define DRV_ETHPHY_LAN8742A_NEG_DONE_TMO            2000
#define DRV_ETHPHY_LAN8742A_RESET_CLR_TMO           500




// *****************************************************************************
// *****************************************************************************
// Section: Application Configuration
// *****************************************************************************
// *****************************************************************************


//DOM-IGNORE-BEGIN
#ifdef __cplusplus
}
#endif
//DOM-IGNORE-END

#endif // CONFIGURATION_H
/*******************************************************************************
 End of File
*/
