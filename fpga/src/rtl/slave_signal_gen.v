//****************************************Copyright (c)***********************************//
//原子哥在线教学平台：www.yuanzige.com
//技术支持：http://www.openedv.com/forum.php
//淘宝店铺：https://zhengdianyuanzi.tmall.com
//关注微信公众平台微信号："正点原子"，免费获取ZYNQ & FPGA & STM32 & LINUX资料。
//版权所有，盗版必究。
//Copyright(C) 正点原子 2023-2033
//All rights reserved                                  
//----------------------------------------------------------------------------------------
// File name:           slave_signal_gen
// Created by:          正点原子
// Created date:        2024年4月15日20:28:08
// Version:             V1.0
// Descriptions:        从机模式下,信号产生模块
//
//----------------------------------------------------------------------------------------
//****************************************************************************************//

module slave_signal_gen(
    input                  clk       ,  // 时钟信号
    input                  rst_n     ,  // 复位信号
        
    //音频接口
    output   reg           aud_bclk  ,  // 位时钟
    output   reg           aud_lrc      // 左右通道时钟
);

//parameter define
//MCLK/LRCK的比率,即分频系数,用于配置音频采样率
//Single Speed模式下,采样率范围8k~50kHz,比率可以设置为256,384,512,768,1024
//aud_lrc的频率对应音频的采样率,12.288/256=48khz
parameter FREQ_DIV = 11'd256 ;  //分频系数 MCLK/LRCK=256

//reg define
reg   [10:0]      lrc_div_cnt;           //左右通道时钟分频计数器
reg   [7:0]       bclk_div_cnt;          //位时钟分频器

//*****************************************************
//**                    main code
//*****************************************************

//根据分频系数,产生aud_lrc时钟,FREQ_DIV/2-1
always @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin
        lrc_div_cnt <= 11'b0;
        aud_lrc <= 1'b0;
    end    
    else begin
        if(lrc_div_cnt == FREQ_DIV[10:1] - 11'b1) begin
            lrc_div_cnt <= 11'b0;
            aud_lrc <= ~aud_lrc;
        end    
        else
            lrc_div_cnt <= lrc_div_cnt + 11'b1;
    end        
end

//产生aud_bclk ,aud_bclk的频率是aud_lrc的64倍,即(FREQ_DIV/64)/2-1
always @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin
        bclk_div_cnt <= 8'b0;
        aud_bclk <= 1'b0;
    end    
    else begin
        if(bclk_div_cnt == 8'd3) begin // 16-bit frame: BCLK = 12.288M/8 = 1.536 MHz -> 32 BCLK/frame
            bclk_div_cnt <= 8'b0;
            aud_bclk <= ~aud_bclk;
        end    
        else
            bclk_div_cnt <= bclk_div_cnt + 8'b1;
    end        
end

endmodule

