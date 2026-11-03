//=============================================================================
// dn_rom.v --- 由 gen_rom.py 从 vec/*.hex 自动生成，请勿手改
//    dn_win_rom : 512 点 sqrt-Hann 合成/分析窗（Q1.15）
//    dn_tw_rom  : 512 点 FFT 旋转因子（Q1.22）
//=============================================================================
`timescale 1ns / 1ps

module dn_win_rom (
    input      [8:0]  addr,
    output reg [15:0] q
);
    always @(*) begin
        case (addr)
            9'd0  : q = 16'h0000;
            9'd1  : q = 16'h00c9;
            9'd2  : q = 16'h0192;
            9'd3  : q = 16'h025b;
            9'd4  : q = 16'h0324;
            9'd5  : q = 16'h03ed;
            9'd6  : q = 16'h04b6;
            9'd7  : q = 16'h057f;
            9'd8  : q = 16'h0648;
            9'd9  : q = 16'h0711;
            9'd10 : q = 16'h07d9;
            9'd11 : q = 16'h08a2;
            9'd12 : q = 16'h096b;
            9'd13 : q = 16'h0a33;
            9'd14 : q = 16'h0afb;
            9'd15 : q = 16'h0bc4;
            9'd16 : q = 16'h0c8c;
            9'd17 : q = 16'h0d54;
            9'd18 : q = 16'h0e1c;
            9'd19 : q = 16'h0ee4;
            9'd20 : q = 16'h0fab;
            9'd21 : q = 16'h1073;
            9'd22 : q = 16'h113a;
            9'd23 : q = 16'h1201;
            9'd24 : q = 16'h12c8;
            9'd25 : q = 16'h138f;
            9'd26 : q = 16'h1455;
            9'd27 : q = 16'h151c;
            9'd28 : q = 16'h15e2;
            9'd29 : q = 16'h16a8;
            9'd30 : q = 16'h176e;
            9'd31 : q = 16'h1833;
            9'd32 : q = 16'h18f9;
            9'd33 : q = 16'h19be;
            9'd34 : q = 16'h1a83;
            9'd35 : q = 16'h1b47;
            9'd36 : q = 16'h1c0c;
            9'd37 : q = 16'h1cd0;
            9'd38 : q = 16'h1d93;
            9'd39 : q = 16'h1e57;
            9'd40 : q = 16'h1f1a;
            9'd41 : q = 16'h1fdd;
            9'd42 : q = 16'h209f;
            9'd43 : q = 16'h2162;
            9'd44 : q = 16'h2224;
            9'd45 : q = 16'h22e5;
            9'd46 : q = 16'h23a7;
            9'd47 : q = 16'h2467;
            9'd48 : q = 16'h2528;
            9'd49 : q = 16'h25e8;
            9'd50 : q = 16'h26a8;
            9'd51 : q = 16'h2768;
            9'd52 : q = 16'h2827;
            9'd53 : q = 16'h28e5;
            9'd54 : q = 16'h29a4;
            9'd55 : q = 16'h2a62;
            9'd56 : q = 16'h2b1f;
            9'd57 : q = 16'h2bdc;
            9'd58 : q = 16'h2c99;
            9'd59 : q = 16'h2d55;
            9'd60 : q = 16'h2e11;
            9'd61 : q = 16'h2ecc;
            9'd62 : q = 16'h2f87;
            9'd63 : q = 16'h3042;
            9'd64 : q = 16'h30fc;
            9'd65 : q = 16'h31b5;
            9'd66 : q = 16'h326e;
            9'd67 : q = 16'h3327;
            9'd68 : q = 16'h33df;
            9'd69 : q = 16'h3497;
            9'd70 : q = 16'h354e;
            9'd71 : q = 16'h3604;
            9'd72 : q = 16'h36ba;
            9'd73 : q = 16'h3770;
            9'd74 : q = 16'h3825;
            9'd75 : q = 16'h38d9;
            9'd76 : q = 16'h398d;
            9'd77 : q = 16'h3a40;
            9'd78 : q = 16'h3af3;
            9'd79 : q = 16'h3ba5;
            9'd80 : q = 16'h3c57;
            9'd81 : q = 16'h3d08;
            9'd82 : q = 16'h3db8;
            9'd83 : q = 16'h3e68;
            9'd84 : q = 16'h3f17;
            9'd85 : q = 16'h3fc6;
            9'd86 : q = 16'h4074;
            9'd87 : q = 16'h4121;
            9'd88 : q = 16'h41ce;
            9'd89 : q = 16'h427a;
            9'd90 : q = 16'h4326;
            9'd91 : q = 16'h43d1;
            9'd92 : q = 16'h447b;
            9'd93 : q = 16'h4524;
            9'd94 : q = 16'h45cd;
            9'd95 : q = 16'h4675;
            9'd96 : q = 16'h471d;
            9'd97 : q = 16'h47c4;
            9'd98 : q = 16'h486a;
            9'd99 : q = 16'h490f;
            9'd100: q = 16'h49b4;
            9'd101: q = 16'h4a58;
            9'd102: q = 16'h4afb;
            9'd103: q = 16'h4b9e;
            9'd104: q = 16'h4c40;
            9'd105: q = 16'h4ce1;
            9'd106: q = 16'h4d81;
            9'd107: q = 16'h4e21;
            9'd108: q = 16'h4ec0;
            9'd109: q = 16'h4f5e;
            9'd110: q = 16'h4ffb;
            9'd111: q = 16'h5098;
            9'd112: q = 16'h5134;
            9'd113: q = 16'h51cf;
            9'd114: q = 16'h5269;
            9'd115: q = 16'h5303;
            9'd116: q = 16'h539b;
            9'd117: q = 16'h5433;
            9'd118: q = 16'h54ca;
            9'd119: q = 16'h5560;
            9'd120: q = 16'h55f6;
            9'd121: q = 16'h568a;
            9'd122: q = 16'h571e;
            9'd123: q = 16'h57b1;
            9'd124: q = 16'h5843;
            9'd125: q = 16'h58d4;
            9'd126: q = 16'h5964;
            9'd127: q = 16'h59f4;
            9'd128: q = 16'h5a82;
            9'd129: q = 16'h5b10;
            9'd130: q = 16'h5b9d;
            9'd131: q = 16'h5c29;
            9'd132: q = 16'h5cb4;
            9'd133: q = 16'h5d3e;
            9'd134: q = 16'h5dc8;
            9'd135: q = 16'h5e50;
            9'd136: q = 16'h5ed7;
            9'd137: q = 16'h5f5e;
            9'd138: q = 16'h5fe4;
            9'd139: q = 16'h6068;
            9'd140: q = 16'h60ec;
            9'd141: q = 16'h616f;
            9'd142: q = 16'h61f1;
            9'd143: q = 16'h6272;
            9'd144: q = 16'h62f2;
            9'd145: q = 16'h6371;
            9'd146: q = 16'h63ef;
            9'd147: q = 16'h646c;
            9'd148: q = 16'h64e9;
            9'd149: q = 16'h6564;
            9'd150: q = 16'h65de;
            9'd151: q = 16'h6657;
            9'd152: q = 16'h66d0;
            9'd153: q = 16'h6747;
            9'd154: q = 16'h67bd;
            9'd155: q = 16'h6832;
            9'd156: q = 16'h68a7;
            9'd157: q = 16'h691a;
            9'd158: q = 16'h698c;
            9'd159: q = 16'h69fd;
            9'd160: q = 16'h6a6e;
            9'd161: q = 16'h6add;
            9'd162: q = 16'h6b4b;
            9'd163: q = 16'h6bb8;
            9'd164: q = 16'h6c24;
            9'd165: q = 16'h6c8f;
            9'd166: q = 16'h6cf9;
            9'd167: q = 16'h6d62;
            9'd168: q = 16'h6dca;
            9'd169: q = 16'h6e31;
            9'd170: q = 16'h6e97;
            9'd171: q = 16'h6efb;
            9'd172: q = 16'h6f5f;
            9'd173: q = 16'h6fc2;
            9'd174: q = 16'h7023;
            9'd175: q = 16'h7083;
            9'd176: q = 16'h70e3;
            9'd177: q = 16'h7141;
            9'd178: q = 16'h719e;
            9'd179: q = 16'h71fa;
            9'd180: q = 16'h7255;
            9'd181: q = 16'h72af;
            9'd182: q = 16'h7308;
            9'd183: q = 16'h735f;
            9'd184: q = 16'h73b6;
            9'd185: q = 16'h740b;
            9'd186: q = 16'h7460;
            9'd187: q = 16'h74b3;
            9'd188: q = 16'h7505;
            9'd189: q = 16'h7556;
            9'd190: q = 16'h75a6;
            9'd191: q = 16'h75f4;
            9'd192: q = 16'h7642;
            9'd193: q = 16'h768e;
            9'd194: q = 16'h76d9;
            9'd195: q = 16'h7723;
            9'd196: q = 16'h776c;
            9'd197: q = 16'h77b4;
            9'd198: q = 16'h77fb;
            9'd199: q = 16'h7840;
            9'd200: q = 16'h7885;
            9'd201: q = 16'h78c8;
            9'd202: q = 16'h790a;
            9'd203: q = 16'h794a;
            9'd204: q = 16'h798a;
            9'd205: q = 16'h79c9;
            9'd206: q = 16'h7a06;
            9'd207: q = 16'h7a42;
            9'd208: q = 16'h7a7d;
            9'd209: q = 16'h7ab7;
            9'd210: q = 16'h7aef;
            9'd211: q = 16'h7b27;
            9'd212: q = 16'h7b5d;
            9'd213: q = 16'h7b92;
            9'd214: q = 16'h7bc6;
            9'd215: q = 16'h7bf9;
            9'd216: q = 16'h7c2a;
            9'd217: q = 16'h7c5a;
            9'd218: q = 16'h7c89;
            9'd219: q = 16'h7cb7;
            9'd220: q = 16'h7ce4;
            9'd221: q = 16'h7d0f;
            9'd222: q = 16'h7d3a;
            9'd223: q = 16'h7d63;
            9'd224: q = 16'h7d8a;
            9'd225: q = 16'h7db1;
            9'd226: q = 16'h7dd6;
            9'd227: q = 16'h7dfb;
            9'd228: q = 16'h7e1e;
            9'd229: q = 16'h7e3f;
            9'd230: q = 16'h7e60;
            9'd231: q = 16'h7e7f;
            9'd232: q = 16'h7e9d;
            9'd233: q = 16'h7eba;
            9'd234: q = 16'h7ed6;
            9'd235: q = 16'h7ef0;
            9'd236: q = 16'h7f0a;
            9'd237: q = 16'h7f22;
            9'd238: q = 16'h7f38;
            9'd239: q = 16'h7f4e;
            9'd240: q = 16'h7f62;
            9'd241: q = 16'h7f75;
            9'd242: q = 16'h7f87;
            9'd243: q = 16'h7f98;
            9'd244: q = 16'h7fa7;
            9'd245: q = 16'h7fb5;
            9'd246: q = 16'h7fc2;
            9'd247: q = 16'h7fce;
            9'd248: q = 16'h7fd9;
            9'd249: q = 16'h7fe2;
            9'd250: q = 16'h7fea;
            9'd251: q = 16'h7ff1;
            9'd252: q = 16'h7ff6;
            9'd253: q = 16'h7ffa;
            9'd254: q = 16'h7ffe;
            9'd255: q = 16'h7fff;
            9'd256: q = 16'h7fff;
            9'd257: q = 16'h7fff;
            9'd258: q = 16'h7ffe;
            9'd259: q = 16'h7ffa;
            9'd260: q = 16'h7ff6;
            9'd261: q = 16'h7ff1;
            9'd262: q = 16'h7fea;
            9'd263: q = 16'h7fe2;
            9'd264: q = 16'h7fd9;
            9'd265: q = 16'h7fce;
            9'd266: q = 16'h7fc2;
            9'd267: q = 16'h7fb5;
            9'd268: q = 16'h7fa7;
            9'd269: q = 16'h7f98;
            9'd270: q = 16'h7f87;
            9'd271: q = 16'h7f75;
            9'd272: q = 16'h7f62;
            9'd273: q = 16'h7f4e;
            9'd274: q = 16'h7f38;
            9'd275: q = 16'h7f22;
            9'd276: q = 16'h7f0a;
            9'd277: q = 16'h7ef0;
            9'd278: q = 16'h7ed6;
            9'd279: q = 16'h7eba;
            9'd280: q = 16'h7e9d;
            9'd281: q = 16'h7e7f;
            9'd282: q = 16'h7e60;
            9'd283: q = 16'h7e3f;
            9'd284: q = 16'h7e1e;
            9'd285: q = 16'h7dfb;
            9'd286: q = 16'h7dd6;
            9'd287: q = 16'h7db1;
            9'd288: q = 16'h7d8a;
            9'd289: q = 16'h7d63;
            9'd290: q = 16'h7d3a;
            9'd291: q = 16'h7d0f;
            9'd292: q = 16'h7ce4;
            9'd293: q = 16'h7cb7;
            9'd294: q = 16'h7c89;
            9'd295: q = 16'h7c5a;
            9'd296: q = 16'h7c2a;
            9'd297: q = 16'h7bf9;
            9'd298: q = 16'h7bc6;
            9'd299: q = 16'h7b92;
            9'd300: q = 16'h7b5d;
            9'd301: q = 16'h7b27;
            9'd302: q = 16'h7aef;
            9'd303: q = 16'h7ab7;
            9'd304: q = 16'h7a7d;
            9'd305: q = 16'h7a42;
            9'd306: q = 16'h7a06;
            9'd307: q = 16'h79c9;
            9'd308: q = 16'h798a;
            9'd309: q = 16'h794a;
            9'd310: q = 16'h790a;
            9'd311: q = 16'h78c8;
            9'd312: q = 16'h7885;
            9'd313: q = 16'h7840;
            9'd314: q = 16'h77fb;
            9'd315: q = 16'h77b4;
            9'd316: q = 16'h776c;
            9'd317: q = 16'h7723;
            9'd318: q = 16'h76d9;
            9'd319: q = 16'h768e;
            9'd320: q = 16'h7642;
            9'd321: q = 16'h75f4;
            9'd322: q = 16'h75a6;
            9'd323: q = 16'h7556;
            9'd324: q = 16'h7505;
            9'd325: q = 16'h74b3;
            9'd326: q = 16'h7460;
            9'd327: q = 16'h740b;
            9'd328: q = 16'h73b6;
            9'd329: q = 16'h735f;
            9'd330: q = 16'h7308;
            9'd331: q = 16'h72af;
            9'd332: q = 16'h7255;
            9'd333: q = 16'h71fa;
            9'd334: q = 16'h719e;
            9'd335: q = 16'h7141;
            9'd336: q = 16'h70e3;
            9'd337: q = 16'h7083;
            9'd338: q = 16'h7023;
            9'd339: q = 16'h6fc2;
            9'd340: q = 16'h6f5f;
            9'd341: q = 16'h6efb;
            9'd342: q = 16'h6e97;
            9'd343: q = 16'h6e31;
            9'd344: q = 16'h6dca;
            9'd345: q = 16'h6d62;
            9'd346: q = 16'h6cf9;
            9'd347: q = 16'h6c8f;
            9'd348: q = 16'h6c24;
            9'd349: q = 16'h6bb8;
            9'd350: q = 16'h6b4b;
            9'd351: q = 16'h6add;
            9'd352: q = 16'h6a6e;
            9'd353: q = 16'h69fd;
            9'd354: q = 16'h698c;
            9'd355: q = 16'h691a;
            9'd356: q = 16'h68a7;
            9'd357: q = 16'h6832;
            9'd358: q = 16'h67bd;
            9'd359: q = 16'h6747;
            9'd360: q = 16'h66d0;
            9'd361: q = 16'h6657;
            9'd362: q = 16'h65de;
            9'd363: q = 16'h6564;
            9'd364: q = 16'h64e9;
            9'd365: q = 16'h646c;
            9'd366: q = 16'h63ef;
            9'd367: q = 16'h6371;
            9'd368: q = 16'h62f2;
            9'd369: q = 16'h6272;
            9'd370: q = 16'h61f1;
            9'd371: q = 16'h616f;
            9'd372: q = 16'h60ec;
            9'd373: q = 16'h6068;
            9'd374: q = 16'h5fe4;
            9'd375: q = 16'h5f5e;
            9'd376: q = 16'h5ed7;
            9'd377: q = 16'h5e50;
            9'd378: q = 16'h5dc8;
            9'd379: q = 16'h5d3e;
            9'd380: q = 16'h5cb4;
            9'd381: q = 16'h5c29;
            9'd382: q = 16'h5b9d;
            9'd383: q = 16'h5b10;
            9'd384: q = 16'h5a82;
            9'd385: q = 16'h59f4;
            9'd386: q = 16'h5964;
            9'd387: q = 16'h58d4;
            9'd388: q = 16'h5843;
            9'd389: q = 16'h57b1;
            9'd390: q = 16'h571e;
            9'd391: q = 16'h568a;
            9'd392: q = 16'h55f6;
            9'd393: q = 16'h5560;
            9'd394: q = 16'h54ca;
            9'd395: q = 16'h5433;
            9'd396: q = 16'h539b;
            9'd397: q = 16'h5303;
            9'd398: q = 16'h5269;
            9'd399: q = 16'h51cf;
            9'd400: q = 16'h5134;
            9'd401: q = 16'h5098;
            9'd402: q = 16'h4ffb;
            9'd403: q = 16'h4f5e;
            9'd404: q = 16'h4ec0;
            9'd405: q = 16'h4e21;
            9'd406: q = 16'h4d81;
            9'd407: q = 16'h4ce1;
            9'd408: q = 16'h4c40;
            9'd409: q = 16'h4b9e;
            9'd410: q = 16'h4afb;
            9'd411: q = 16'h4a58;
            9'd412: q = 16'h49b4;
            9'd413: q = 16'h490f;
            9'd414: q = 16'h486a;
            9'd415: q = 16'h47c4;
            9'd416: q = 16'h471d;
            9'd417: q = 16'h4675;
            9'd418: q = 16'h45cd;
            9'd419: q = 16'h4524;
            9'd420: q = 16'h447b;
            9'd421: q = 16'h43d1;
            9'd422: q = 16'h4326;
            9'd423: q = 16'h427a;
            9'd424: q = 16'h41ce;
            9'd425: q = 16'h4121;
            9'd426: q = 16'h4074;
            9'd427: q = 16'h3fc6;
            9'd428: q = 16'h3f17;
            9'd429: q = 16'h3e68;
            9'd430: q = 16'h3db8;
            9'd431: q = 16'h3d08;
            9'd432: q = 16'h3c57;
            9'd433: q = 16'h3ba5;
            9'd434: q = 16'h3af3;
            9'd435: q = 16'h3a40;
            9'd436: q = 16'h398d;
            9'd437: q = 16'h38d9;
            9'd438: q = 16'h3825;
            9'd439: q = 16'h3770;
            9'd440: q = 16'h36ba;
            9'd441: q = 16'h3604;
            9'd442: q = 16'h354e;
            9'd443: q = 16'h3497;
            9'd444: q = 16'h33df;
            9'd445: q = 16'h3327;
            9'd446: q = 16'h326e;
            9'd447: q = 16'h31b5;
            9'd448: q = 16'h30fc;
            9'd449: q = 16'h3042;
            9'd450: q = 16'h2f87;
            9'd451: q = 16'h2ecc;
            9'd452: q = 16'h2e11;
            9'd453: q = 16'h2d55;
            9'd454: q = 16'h2c99;
            9'd455: q = 16'h2bdc;
            9'd456: q = 16'h2b1f;
            9'd457: q = 16'h2a62;
            9'd458: q = 16'h29a4;
            9'd459: q = 16'h28e5;
            9'd460: q = 16'h2827;
            9'd461: q = 16'h2768;
            9'd462: q = 16'h26a8;
            9'd463: q = 16'h25e8;
            9'd464: q = 16'h2528;
            9'd465: q = 16'h2467;
            9'd466: q = 16'h23a7;
            9'd467: q = 16'h22e5;
            9'd468: q = 16'h2224;
            9'd469: q = 16'h2162;
            9'd470: q = 16'h209f;
            9'd471: q = 16'h1fdd;
            9'd472: q = 16'h1f1a;
            9'd473: q = 16'h1e57;
            9'd474: q = 16'h1d93;
            9'd475: q = 16'h1cd0;
            9'd476: q = 16'h1c0c;
            9'd477: q = 16'h1b47;
            9'd478: q = 16'h1a83;
            9'd479: q = 16'h19be;
            9'd480: q = 16'h18f9;
            9'd481: q = 16'h1833;
            9'd482: q = 16'h176e;
            9'd483: q = 16'h16a8;
            9'd484: q = 16'h15e2;
            9'd485: q = 16'h151c;
            9'd486: q = 16'h1455;
            9'd487: q = 16'h138f;
            9'd488: q = 16'h12c8;
            9'd489: q = 16'h1201;
            9'd490: q = 16'h113a;
            9'd491: q = 16'h1073;
            9'd492: q = 16'h0fab;
            9'd493: q = 16'h0ee4;
            9'd494: q = 16'h0e1c;
            9'd495: q = 16'h0d54;
            9'd496: q = 16'h0c8c;
            9'd497: q = 16'h0bc4;
            9'd498: q = 16'h0afb;
            9'd499: q = 16'h0a33;
            9'd500: q = 16'h096b;
            9'd501: q = 16'h08a2;
            9'd502: q = 16'h07d9;
            9'd503: q = 16'h0711;
            9'd504: q = 16'h0648;
            9'd505: q = 16'h057f;
            9'd506: q = 16'h04b6;
            9'd507: q = 16'h03ed;
            9'd508: q = 16'h0324;
            9'd509: q = 16'h025b;
            9'd510: q = 16'h0192;
            9'd511: q = 16'h00c9;
            default: q = 16'h0000;
        endcase
    end
endmodule

module dn_tw_rom (
    input      [8:0]  addr,
    output reg [23:0] c,
    output reg [23:0] s
);
    always @(*) begin
        case (addr)
            9'd0  : begin c = 24'h400000; s = 24'h000000; end
            9'd1  : begin c = 24'h3ffec4; s = 24'hff36f1; end
            9'd2  : begin c = 24'h3ffb11; s = 24'hfe6deb; end
            9'd3  : begin c = 24'h3ff4e6; s = 24'hfda4f3; end
            9'd4  : begin c = 24'h3fec44; s = 24'hfcdc13; end
            9'd5  : begin c = 24'h3fe12b; s = 24'hfc1352; end
            9'd6  : begin c = 24'h3fd39b; s = 24'hfb4ab8; end
            9'd7  : begin c = 24'h3fc396; s = 24'hfa824c; end
            9'd8  : begin c = 24'h3fb11b; s = 24'hf9ba16; end
            9'd9  : begin c = 24'h3f9c2c; s = 24'hf8f21f; end
            9'd10 : begin c = 24'h3f84c9; s = 24'hf82a6c; end
            9'd11 : begin c = 24'h3f6af3; s = 24'hf76308; end
            9'd12 : begin c = 24'h3f4eab; s = 24'hf69bf8; end
            9'd13 : begin c = 24'h3f2ff2; s = 24'hf5d545; end
            9'd14 : begin c = 24'h3f0eca; s = 24'hf50ef6; end
            9'd15 : begin c = 24'h3eeb33; s = 24'hf44913; end
            9'd16 : begin c = 24'h3ec530; s = 24'hf383a4; end
            9'd17 : begin c = 24'h3e9cc0; s = 24'hf2beb0; end
            9'd18 : begin c = 24'h3e71e7; s = 24'hf1fa3f; end
            9'd19 : begin c = 24'h3e44a6; s = 24'hf13658; end
            9'd20 : begin c = 24'h3e14fe; s = 24'hf07303; end
            9'd21 : begin c = 24'h3de2f1; s = 24'hefb048; end
            9'd22 : begin c = 24'h3dae82; s = 24'heeee2e; end
            9'd23 : begin c = 24'h3d77b2; s = 24'hee2cbc; end
            9'd24 : begin c = 24'h3d3e83; s = 24'hed6bfa; end
            9'd25 : begin c = 24'h3d02f7; s = 24'hecabef; end
            9'd26 : begin c = 24'h3cc512; s = 24'hebeca3; end
            9'd27 : begin c = 24'h3c84d5; s = 24'heb2e1e; end
            9'd28 : begin c = 24'h3c4242; s = 24'hea7066; end
            9'd29 : begin c = 24'h3bfd5d; s = 24'he9b382; end
            9'd30 : begin c = 24'h3bb627; s = 24'he8f77b; end
            9'd31 : begin c = 24'h3b6ca5; s = 24'he83c57; end
            9'd32 : begin c = 24'h3b20d8; s = 24'he7821d; end
            9'd33 : begin c = 24'h3ad2c3; s = 24'he6c8d6; end
            9'd34 : begin c = 24'h3a826a; s = 24'he61087; end
            9'd35 : begin c = 24'h3a2fcf; s = 24'he55938; end
            9'd36 : begin c = 24'h39daf6; s = 24'he4a2f0; end
            9'd37 : begin c = 24'h3983e2; s = 24'he3edb6; end
            9'd38 : begin c = 24'h392a96; s = 24'he33991; end
            9'd39 : begin c = 24'h38cf16; s = 24'he28689; end
            9'd40 : begin c = 24'h387166; s = 24'he1d4a3; end
            9'd41 : begin c = 24'h381188; s = 24'he123e7; end
            9'd42 : begin c = 24'h37af81; s = 24'he0745b; end
            9'd43 : begin c = 24'h374b55; s = 24'hdfc607; end
            9'd44 : begin c = 24'h36e507; s = 24'hdf18f1; end
            9'd45 : begin c = 24'h367c9a; s = 24'hde6d1f; end
            9'd46 : begin c = 24'h361215; s = 24'hddc299; end
            9'd47 : begin c = 24'h35a579; s = 24'hdd1965; end
            9'd48 : begin c = 24'h3536cc; s = 24'hdc718a; end
            9'd49 : begin c = 24'h34c612; s = 24'hdbcb0d; end
            9'd50 : begin c = 24'h34534f; s = 24'hdb25f5; end
            9'd51 : begin c = 24'h33de88; s = 24'hda824a; end
            9'd52 : begin c = 24'h3367c1; s = 24'hd9e010; end
            9'd53 : begin c = 24'h32eefe; s = 24'hd93f4f; end
            9'd54 : begin c = 24'h327445; s = 24'hd8a00c; end
            9'd55 : begin c = 24'h31f799; s = 24'hd8024d; end
            9'd56 : begin c = 24'h317901; s = 24'hd7661a; end
            9'd57 : begin c = 24'h30f880; s = 24'hd6cb77; end
            9'd58 : begin c = 24'h30761c; s = 24'hd6326b; end
            9'd59 : begin c = 24'h2ff1da; s = 24'hd59afb; end
            9'd60 : begin c = 24'h2f6bbe; s = 24'hd5052e; end
            9'd61 : begin c = 24'h2ee3cf; s = 24'hd47109; end
            9'd62 : begin c = 24'h2e5a10; s = 24'hd3de91; end
            9'd63 : begin c = 24'h2dce89; s = 24'hd34dce; end
            9'd64 : begin c = 24'h2d413d; s = 24'hd2bec3; end
            9'd65 : begin c = 24'h2cb232; s = 24'hd23177; end
            9'd66 : begin c = 24'h2c216f; s = 24'hd1a5f0; end
            9'd67 : begin c = 24'h2b8ef7; s = 24'hd11c31; end
            9'd68 : begin c = 24'h2afad2; s = 24'hd09442; end
            9'd69 : begin c = 24'h2a6505; s = 24'hd00e26; end
            9'd70 : begin c = 24'h29cd95; s = 24'hcf89e4; end
            9'd71 : begin c = 24'h293489; s = 24'hcf0780; end
            9'd72 : begin c = 24'h2899e6; s = 24'hce86ff; end
            9'd73 : begin c = 24'h27fdb3; s = 24'hce0867; end
            9'd74 : begin c = 24'h275ff4; s = 24'hcd8bbb; end
            9'd75 : begin c = 24'h26c0b1; s = 24'hcd1102; end
            9'd76 : begin c = 24'h261ff0; s = 24'hcc983f; end
            9'd77 : begin c = 24'h257db6; s = 24'hcc2178; end
            9'd78 : begin c = 24'h24da0b; s = 24'hcbacb1; end
            9'd79 : begin c = 24'h2434f3; s = 24'hcb39ee; end
            9'd80 : begin c = 24'h238e76; s = 24'hcac934; end
            9'd81 : begin c = 24'h22e69b; s = 24'hca5a87; end
            9'd82 : begin c = 24'h223d67; s = 24'hc9edeb; end
            9'd83 : begin c = 24'h2192e1; s = 24'hc98366; end
            9'd84 : begin c = 24'h20e70f; s = 24'hc91af9; end
            9'd85 : begin c = 24'h2039f9; s = 24'hc8b4ab; end
            9'd86 : begin c = 24'h1f8ba5; s = 24'hc8507f; end
            9'd87 : begin c = 24'h1edc19; s = 24'hc7ee78; end
            9'd88 : begin c = 24'h1e2b5d; s = 24'hc78e9a; end
            9'd89 : begin c = 24'h1d7977; s = 24'hc730ea; end
            9'd90 : begin c = 24'h1cc66f; s = 24'hc6d56a; end
            9'd91 : begin c = 24'h1c124a; s = 24'hc67c1e; end
            9'd92 : begin c = 24'h1b5d10; s = 24'hc6250a; end
            9'd93 : begin c = 24'h1aa6c8; s = 24'hc5d031; end
            9'd94 : begin c = 24'h19ef79; s = 24'hc57d96; end
            9'd95 : begin c = 24'h19372a; s = 24'hc52d3d; end
            9'd96 : begin c = 24'h187de3; s = 24'hc4df28; end
            9'd97 : begin c = 24'h17c3a9; s = 24'hc4935b; end
            9'd98 : begin c = 24'h170885; s = 24'hc449d9; end
            9'd99 : begin c = 24'h164c7e; s = 24'hc402a3; end
            9'd100: begin c = 24'h158f9a; s = 24'hc3bdbe; end
            9'd101: begin c = 24'h14d1e2; s = 24'hc37b2b; end
            9'd102: begin c = 24'h14135d; s = 24'hc33aee; end
            9'd103: begin c = 24'h135411; s = 24'hc2fd09; end
            9'd104: begin c = 24'h129406; s = 24'hc2c17d; end
            9'd105: begin c = 24'h11d344; s = 24'hc2884e; end
            9'd106: begin c = 24'h1111d2; s = 24'hc2517e; end
            9'd107: begin c = 24'h104fb8; s = 24'hc21d0f; end
            9'd108: begin c = 24'h0f8cfd; s = 24'hc1eb02; end
            9'd109: begin c = 24'h0ec9a8; s = 24'hc1bb5a; end
            9'd110: begin c = 24'h0e05c1; s = 24'hc18e19; end
            9'd111: begin c = 24'h0d4150; s = 24'hc16340; end
            9'd112: begin c = 24'h0c7c5c; s = 24'hc13ad0; end
            9'd113: begin c = 24'h0bb6ed; s = 24'hc114cd; end
            9'd114: begin c = 24'h0af10a; s = 24'hc0f136; end
            9'd115: begin c = 24'h0a2abb; s = 24'hc0d00e; end
            9'd116: begin c = 24'h096408; s = 24'hc0b155; end
            9'd117: begin c = 24'h089cf8; s = 24'hc0950d; end
            9'd118: begin c = 24'h07d594; s = 24'hc07b37; end
            9'd119: begin c = 24'h070de1; s = 24'hc063d4; end
            9'd120: begin c = 24'h0645ea; s = 24'hc04ee5; end
            9'd121: begin c = 24'h057db4; s = 24'hc03c6a; end
            9'd122: begin c = 24'h04b548; s = 24'hc02c65; end
            9'd123: begin c = 24'h03ecae; s = 24'hc01ed5; end
            9'd124: begin c = 24'h0323ed; s = 24'hc013bc; end
            9'd125: begin c = 24'h025b0d; s = 24'hc00b1a; end
            9'd126: begin c = 24'h019215; s = 24'hc004ef; end
            9'd127: begin c = 24'h00c90f; s = 24'hc0013c; end
            9'd128: begin c = 24'h000000; s = 24'hc00000; end
            9'd129: begin c = 24'hff36f1; s = 24'hc0013c; end
            9'd130: begin c = 24'hfe6deb; s = 24'hc004ef; end
            9'd131: begin c = 24'hfda4f3; s = 24'hc00b1a; end
            9'd132: begin c = 24'hfcdc13; s = 24'hc013bc; end
            9'd133: begin c = 24'hfc1352; s = 24'hc01ed5; end
            9'd134: begin c = 24'hfb4ab8; s = 24'hc02c65; end
            9'd135: begin c = 24'hfa824c; s = 24'hc03c6a; end
            9'd136: begin c = 24'hf9ba16; s = 24'hc04ee5; end
            9'd137: begin c = 24'hf8f21f; s = 24'hc063d4; end
            9'd138: begin c = 24'hf82a6c; s = 24'hc07b37; end
            9'd139: begin c = 24'hf76308; s = 24'hc0950d; end
            9'd140: begin c = 24'hf69bf8; s = 24'hc0b155; end
            9'd141: begin c = 24'hf5d545; s = 24'hc0d00e; end
            9'd142: begin c = 24'hf50ef6; s = 24'hc0f136; end
            9'd143: begin c = 24'hf44913; s = 24'hc114cd; end
            9'd144: begin c = 24'hf383a4; s = 24'hc13ad0; end
            9'd145: begin c = 24'hf2beb0; s = 24'hc16340; end
            9'd146: begin c = 24'hf1fa3f; s = 24'hc18e19; end
            9'd147: begin c = 24'hf13658; s = 24'hc1bb5a; end
            9'd148: begin c = 24'hf07303; s = 24'hc1eb02; end
            9'd149: begin c = 24'hefb048; s = 24'hc21d0f; end
            9'd150: begin c = 24'heeee2e; s = 24'hc2517e; end
            9'd151: begin c = 24'hee2cbc; s = 24'hc2884e; end
            9'd152: begin c = 24'hed6bfa; s = 24'hc2c17d; end
            9'd153: begin c = 24'hecabef; s = 24'hc2fd09; end
            9'd154: begin c = 24'hebeca3; s = 24'hc33aee; end
            9'd155: begin c = 24'heb2e1e; s = 24'hc37b2b; end
            9'd156: begin c = 24'hea7066; s = 24'hc3bdbe; end
            9'd157: begin c = 24'he9b382; s = 24'hc402a3; end
            9'd158: begin c = 24'he8f77b; s = 24'hc449d9; end
            9'd159: begin c = 24'he83c57; s = 24'hc4935b; end
            9'd160: begin c = 24'he7821d; s = 24'hc4df28; end
            9'd161: begin c = 24'he6c8d6; s = 24'hc52d3d; end
            9'd162: begin c = 24'he61087; s = 24'hc57d96; end
            9'd163: begin c = 24'he55938; s = 24'hc5d031; end
            9'd164: begin c = 24'he4a2f0; s = 24'hc6250a; end
            9'd165: begin c = 24'he3edb6; s = 24'hc67c1e; end
            9'd166: begin c = 24'he33991; s = 24'hc6d56a; end
            9'd167: begin c = 24'he28689; s = 24'hc730ea; end
            9'd168: begin c = 24'he1d4a3; s = 24'hc78e9a; end
            9'd169: begin c = 24'he123e7; s = 24'hc7ee78; end
            9'd170: begin c = 24'he0745b; s = 24'hc8507f; end
            9'd171: begin c = 24'hdfc607; s = 24'hc8b4ab; end
            9'd172: begin c = 24'hdf18f1; s = 24'hc91af9; end
            9'd173: begin c = 24'hde6d1f; s = 24'hc98366; end
            9'd174: begin c = 24'hddc299; s = 24'hc9edeb; end
            9'd175: begin c = 24'hdd1965; s = 24'hca5a87; end
            9'd176: begin c = 24'hdc718a; s = 24'hcac934; end
            9'd177: begin c = 24'hdbcb0d; s = 24'hcb39ee; end
            9'd178: begin c = 24'hdb25f5; s = 24'hcbacb1; end
            9'd179: begin c = 24'hda824a; s = 24'hcc2178; end
            9'd180: begin c = 24'hd9e010; s = 24'hcc983f; end
            9'd181: begin c = 24'hd93f4f; s = 24'hcd1102; end
            9'd182: begin c = 24'hd8a00c; s = 24'hcd8bbb; end
            9'd183: begin c = 24'hd8024d; s = 24'hce0867; end
            9'd184: begin c = 24'hd7661a; s = 24'hce86ff; end
            9'd185: begin c = 24'hd6cb77; s = 24'hcf0780; end
            9'd186: begin c = 24'hd6326b; s = 24'hcf89e4; end
            9'd187: begin c = 24'hd59afb; s = 24'hd00e26; end
            9'd188: begin c = 24'hd5052e; s = 24'hd09442; end
            9'd189: begin c = 24'hd47109; s = 24'hd11c31; end
            9'd190: begin c = 24'hd3de91; s = 24'hd1a5f0; end
            9'd191: begin c = 24'hd34dce; s = 24'hd23177; end
            9'd192: begin c = 24'hd2bec3; s = 24'hd2bec3; end
            9'd193: begin c = 24'hd23177; s = 24'hd34dce; end
            9'd194: begin c = 24'hd1a5f0; s = 24'hd3de91; end
            9'd195: begin c = 24'hd11c31; s = 24'hd47109; end
            9'd196: begin c = 24'hd09442; s = 24'hd5052e; end
            9'd197: begin c = 24'hd00e26; s = 24'hd59afb; end
            9'd198: begin c = 24'hcf89e4; s = 24'hd6326b; end
            9'd199: begin c = 24'hcf0780; s = 24'hd6cb77; end
            9'd200: begin c = 24'hce86ff; s = 24'hd7661a; end
            9'd201: begin c = 24'hce0867; s = 24'hd8024d; end
            9'd202: begin c = 24'hcd8bbb; s = 24'hd8a00c; end
            9'd203: begin c = 24'hcd1102; s = 24'hd93f4f; end
            9'd204: begin c = 24'hcc983f; s = 24'hd9e010; end
            9'd205: begin c = 24'hcc2178; s = 24'hda824a; end
            9'd206: begin c = 24'hcbacb1; s = 24'hdb25f5; end
            9'd207: begin c = 24'hcb39ee; s = 24'hdbcb0d; end
            9'd208: begin c = 24'hcac934; s = 24'hdc718a; end
            9'd209: begin c = 24'hca5a87; s = 24'hdd1965; end
            9'd210: begin c = 24'hc9edeb; s = 24'hddc299; end
            9'd211: begin c = 24'hc98366; s = 24'hde6d1f; end
            9'd212: begin c = 24'hc91af9; s = 24'hdf18f1; end
            9'd213: begin c = 24'hc8b4ab; s = 24'hdfc607; end
            9'd214: begin c = 24'hc8507f; s = 24'he0745b; end
            9'd215: begin c = 24'hc7ee78; s = 24'he123e7; end
            9'd216: begin c = 24'hc78e9a; s = 24'he1d4a3; end
            9'd217: begin c = 24'hc730ea; s = 24'he28689; end
            9'd218: begin c = 24'hc6d56a; s = 24'he33991; end
            9'd219: begin c = 24'hc67c1e; s = 24'he3edb6; end
            9'd220: begin c = 24'hc6250a; s = 24'he4a2f0; end
            9'd221: begin c = 24'hc5d031; s = 24'he55938; end
            9'd222: begin c = 24'hc57d96; s = 24'he61087; end
            9'd223: begin c = 24'hc52d3d; s = 24'he6c8d6; end
            9'd224: begin c = 24'hc4df28; s = 24'he7821d; end
            9'd225: begin c = 24'hc4935b; s = 24'he83c57; end
            9'd226: begin c = 24'hc449d9; s = 24'he8f77b; end
            9'd227: begin c = 24'hc402a3; s = 24'he9b382; end
            9'd228: begin c = 24'hc3bdbe; s = 24'hea7066; end
            9'd229: begin c = 24'hc37b2b; s = 24'heb2e1e; end
            9'd230: begin c = 24'hc33aee; s = 24'hebeca3; end
            9'd231: begin c = 24'hc2fd09; s = 24'hecabef; end
            9'd232: begin c = 24'hc2c17d; s = 24'hed6bfa; end
            9'd233: begin c = 24'hc2884e; s = 24'hee2cbc; end
            9'd234: begin c = 24'hc2517e; s = 24'heeee2e; end
            9'd235: begin c = 24'hc21d0f; s = 24'hefb048; end
            9'd236: begin c = 24'hc1eb02; s = 24'hf07303; end
            9'd237: begin c = 24'hc1bb5a; s = 24'hf13658; end
            9'd238: begin c = 24'hc18e19; s = 24'hf1fa3f; end
            9'd239: begin c = 24'hc16340; s = 24'hf2beb0; end
            9'd240: begin c = 24'hc13ad0; s = 24'hf383a4; end
            9'd241: begin c = 24'hc114cd; s = 24'hf44913; end
            9'd242: begin c = 24'hc0f136; s = 24'hf50ef6; end
            9'd243: begin c = 24'hc0d00e; s = 24'hf5d545; end
            9'd244: begin c = 24'hc0b155; s = 24'hf69bf8; end
            9'd245: begin c = 24'hc0950d; s = 24'hf76308; end
            9'd246: begin c = 24'hc07b37; s = 24'hf82a6c; end
            9'd247: begin c = 24'hc063d4; s = 24'hf8f21f; end
            9'd248: begin c = 24'hc04ee5; s = 24'hf9ba16; end
            9'd249: begin c = 24'hc03c6a; s = 24'hfa824c; end
            9'd250: begin c = 24'hc02c65; s = 24'hfb4ab8; end
            9'd251: begin c = 24'hc01ed5; s = 24'hfc1352; end
            9'd252: begin c = 24'hc013bc; s = 24'hfcdc13; end
            9'd253: begin c = 24'hc00b1a; s = 24'hfda4f3; end
            9'd254: begin c = 24'hc004ef; s = 24'hfe6deb; end
            9'd255: begin c = 24'hc0013c; s = 24'hff36f1; end
            9'd256: begin c = 24'hc00000; s = 24'h000000; end
            9'd257: begin c = 24'hc0013c; s = 24'h00c90f; end
            9'd258: begin c = 24'hc004ef; s = 24'h019215; end
            9'd259: begin c = 24'hc00b1a; s = 24'h025b0d; end
            9'd260: begin c = 24'hc013bc; s = 24'h0323ed; end
            9'd261: begin c = 24'hc01ed5; s = 24'h03ecae; end
            9'd262: begin c = 24'hc02c65; s = 24'h04b548; end
            9'd263: begin c = 24'hc03c6a; s = 24'h057db4; end
            9'd264: begin c = 24'hc04ee5; s = 24'h0645ea; end
            9'd265: begin c = 24'hc063d4; s = 24'h070de1; end
            9'd266: begin c = 24'hc07b37; s = 24'h07d594; end
            9'd267: begin c = 24'hc0950d; s = 24'h089cf8; end
            9'd268: begin c = 24'hc0b155; s = 24'h096408; end
            9'd269: begin c = 24'hc0d00e; s = 24'h0a2abb; end
            9'd270: begin c = 24'hc0f136; s = 24'h0af10a; end
            9'd271: begin c = 24'hc114cd; s = 24'h0bb6ed; end
            9'd272: begin c = 24'hc13ad0; s = 24'h0c7c5c; end
            9'd273: begin c = 24'hc16340; s = 24'h0d4150; end
            9'd274: begin c = 24'hc18e19; s = 24'h0e05c1; end
            9'd275: begin c = 24'hc1bb5a; s = 24'h0ec9a8; end
            9'd276: begin c = 24'hc1eb02; s = 24'h0f8cfd; end
            9'd277: begin c = 24'hc21d0f; s = 24'h104fb8; end
            9'd278: begin c = 24'hc2517e; s = 24'h1111d2; end
            9'd279: begin c = 24'hc2884e; s = 24'h11d344; end
            9'd280: begin c = 24'hc2c17d; s = 24'h129406; end
            9'd281: begin c = 24'hc2fd09; s = 24'h135411; end
            9'd282: begin c = 24'hc33aee; s = 24'h14135d; end
            9'd283: begin c = 24'hc37b2b; s = 24'h14d1e2; end
            9'd284: begin c = 24'hc3bdbe; s = 24'h158f9a; end
            9'd285: begin c = 24'hc402a3; s = 24'h164c7e; end
            9'd286: begin c = 24'hc449d9; s = 24'h170885; end
            9'd287: begin c = 24'hc4935b; s = 24'h17c3a9; end
            9'd288: begin c = 24'hc4df28; s = 24'h187de3; end
            9'd289: begin c = 24'hc52d3d; s = 24'h19372a; end
            9'd290: begin c = 24'hc57d96; s = 24'h19ef79; end
            9'd291: begin c = 24'hc5d031; s = 24'h1aa6c8; end
            9'd292: begin c = 24'hc6250a; s = 24'h1b5d10; end
            9'd293: begin c = 24'hc67c1e; s = 24'h1c124a; end
            9'd294: begin c = 24'hc6d56a; s = 24'h1cc66f; end
            9'd295: begin c = 24'hc730ea; s = 24'h1d7977; end
            9'd296: begin c = 24'hc78e9a; s = 24'h1e2b5d; end
            9'd297: begin c = 24'hc7ee78; s = 24'h1edc19; end
            9'd298: begin c = 24'hc8507f; s = 24'h1f8ba5; end
            9'd299: begin c = 24'hc8b4ab; s = 24'h2039f9; end
            9'd300: begin c = 24'hc91af9; s = 24'h20e70f; end
            9'd301: begin c = 24'hc98366; s = 24'h2192e1; end
            9'd302: begin c = 24'hc9edeb; s = 24'h223d67; end
            9'd303: begin c = 24'hca5a87; s = 24'h22e69b; end
            9'd304: begin c = 24'hcac934; s = 24'h238e76; end
            9'd305: begin c = 24'hcb39ee; s = 24'h2434f3; end
            9'd306: begin c = 24'hcbacb1; s = 24'h24da0b; end
            9'd307: begin c = 24'hcc2178; s = 24'h257db6; end
            9'd308: begin c = 24'hcc983f; s = 24'h261ff0; end
            9'd309: begin c = 24'hcd1102; s = 24'h26c0b1; end
            9'd310: begin c = 24'hcd8bbb; s = 24'h275ff4; end
            9'd311: begin c = 24'hce0867; s = 24'h27fdb3; end
            9'd312: begin c = 24'hce86ff; s = 24'h2899e6; end
            9'd313: begin c = 24'hcf0780; s = 24'h293489; end
            9'd314: begin c = 24'hcf89e4; s = 24'h29cd95; end
            9'd315: begin c = 24'hd00e26; s = 24'h2a6505; end
            9'd316: begin c = 24'hd09442; s = 24'h2afad2; end
            9'd317: begin c = 24'hd11c31; s = 24'h2b8ef7; end
            9'd318: begin c = 24'hd1a5f0; s = 24'h2c216f; end
            9'd319: begin c = 24'hd23177; s = 24'h2cb232; end
            9'd320: begin c = 24'hd2bec3; s = 24'h2d413d; end
            9'd321: begin c = 24'hd34dce; s = 24'h2dce89; end
            9'd322: begin c = 24'hd3de91; s = 24'h2e5a10; end
            9'd323: begin c = 24'hd47109; s = 24'h2ee3cf; end
            9'd324: begin c = 24'hd5052e; s = 24'h2f6bbe; end
            9'd325: begin c = 24'hd59afb; s = 24'h2ff1da; end
            9'd326: begin c = 24'hd6326b; s = 24'h30761c; end
            9'd327: begin c = 24'hd6cb77; s = 24'h30f880; end
            9'd328: begin c = 24'hd7661a; s = 24'h317901; end
            9'd329: begin c = 24'hd8024d; s = 24'h31f799; end
            9'd330: begin c = 24'hd8a00c; s = 24'h327445; end
            9'd331: begin c = 24'hd93f4f; s = 24'h32eefe; end
            9'd332: begin c = 24'hd9e010; s = 24'h3367c1; end
            9'd333: begin c = 24'hda824a; s = 24'h33de88; end
            9'd334: begin c = 24'hdb25f5; s = 24'h34534f; end
            9'd335: begin c = 24'hdbcb0d; s = 24'h34c612; end
            9'd336: begin c = 24'hdc718a; s = 24'h3536cc; end
            9'd337: begin c = 24'hdd1965; s = 24'h35a579; end
            9'd338: begin c = 24'hddc299; s = 24'h361215; end
            9'd339: begin c = 24'hde6d1f; s = 24'h367c9a; end
            9'd340: begin c = 24'hdf18f1; s = 24'h36e507; end
            9'd341: begin c = 24'hdfc607; s = 24'h374b55; end
            9'd342: begin c = 24'he0745b; s = 24'h37af81; end
            9'd343: begin c = 24'he123e7; s = 24'h381188; end
            9'd344: begin c = 24'he1d4a3; s = 24'h387166; end
            9'd345: begin c = 24'he28689; s = 24'h38cf16; end
            9'd346: begin c = 24'he33991; s = 24'h392a96; end
            9'd347: begin c = 24'he3edb6; s = 24'h3983e2; end
            9'd348: begin c = 24'he4a2f0; s = 24'h39daf6; end
            9'd349: begin c = 24'he55938; s = 24'h3a2fcf; end
            9'd350: begin c = 24'he61087; s = 24'h3a826a; end
            9'd351: begin c = 24'he6c8d6; s = 24'h3ad2c3; end
            9'd352: begin c = 24'he7821d; s = 24'h3b20d8; end
            9'd353: begin c = 24'he83c57; s = 24'h3b6ca5; end
            9'd354: begin c = 24'he8f77b; s = 24'h3bb627; end
            9'd355: begin c = 24'he9b382; s = 24'h3bfd5d; end
            9'd356: begin c = 24'hea7066; s = 24'h3c4242; end
            9'd357: begin c = 24'heb2e1e; s = 24'h3c84d5; end
            9'd358: begin c = 24'hebeca3; s = 24'h3cc512; end
            9'd359: begin c = 24'hecabef; s = 24'h3d02f7; end
            9'd360: begin c = 24'hed6bfa; s = 24'h3d3e83; end
            9'd361: begin c = 24'hee2cbc; s = 24'h3d77b2; end
            9'd362: begin c = 24'heeee2e; s = 24'h3dae82; end
            9'd363: begin c = 24'hefb048; s = 24'h3de2f1; end
            9'd364: begin c = 24'hf07303; s = 24'h3e14fe; end
            9'd365: begin c = 24'hf13658; s = 24'h3e44a6; end
            9'd366: begin c = 24'hf1fa3f; s = 24'h3e71e7; end
            9'd367: begin c = 24'hf2beb0; s = 24'h3e9cc0; end
            9'd368: begin c = 24'hf383a4; s = 24'h3ec530; end
            9'd369: begin c = 24'hf44913; s = 24'h3eeb33; end
            9'd370: begin c = 24'hf50ef6; s = 24'h3f0eca; end
            9'd371: begin c = 24'hf5d545; s = 24'h3f2ff2; end
            9'd372: begin c = 24'hf69bf8; s = 24'h3f4eab; end
            9'd373: begin c = 24'hf76308; s = 24'h3f6af3; end
            9'd374: begin c = 24'hf82a6c; s = 24'h3f84c9; end
            9'd375: begin c = 24'hf8f21f; s = 24'h3f9c2c; end
            9'd376: begin c = 24'hf9ba16; s = 24'h3fb11b; end
            9'd377: begin c = 24'hfa824c; s = 24'h3fc396; end
            9'd378: begin c = 24'hfb4ab8; s = 24'h3fd39b; end
            9'd379: begin c = 24'hfc1352; s = 24'h3fe12b; end
            9'd380: begin c = 24'hfcdc13; s = 24'h3fec44; end
            9'd381: begin c = 24'hfda4f3; s = 24'h3ff4e6; end
            9'd382: begin c = 24'hfe6deb; s = 24'h3ffb11; end
            9'd383: begin c = 24'hff36f1; s = 24'h3ffec4; end
            9'd384: begin c = 24'h000000; s = 24'h400000; end
            9'd385: begin c = 24'h00c90f; s = 24'h3ffec4; end
            9'd386: begin c = 24'h019215; s = 24'h3ffb11; end
            9'd387: begin c = 24'h025b0d; s = 24'h3ff4e6; end
            9'd388: begin c = 24'h0323ed; s = 24'h3fec44; end
            9'd389: begin c = 24'h03ecae; s = 24'h3fe12b; end
            9'd390: begin c = 24'h04b548; s = 24'h3fd39b; end
            9'd391: begin c = 24'h057db4; s = 24'h3fc396; end
            9'd392: begin c = 24'h0645ea; s = 24'h3fb11b; end
            9'd393: begin c = 24'h070de1; s = 24'h3f9c2c; end
            9'd394: begin c = 24'h07d594; s = 24'h3f84c9; end
            9'd395: begin c = 24'h089cf8; s = 24'h3f6af3; end
            9'd396: begin c = 24'h096408; s = 24'h3f4eab; end
            9'd397: begin c = 24'h0a2abb; s = 24'h3f2ff2; end
            9'd398: begin c = 24'h0af10a; s = 24'h3f0eca; end
            9'd399: begin c = 24'h0bb6ed; s = 24'h3eeb33; end
            9'd400: begin c = 24'h0c7c5c; s = 24'h3ec530; end
            9'd401: begin c = 24'h0d4150; s = 24'h3e9cc0; end
            9'd402: begin c = 24'h0e05c1; s = 24'h3e71e7; end
            9'd403: begin c = 24'h0ec9a8; s = 24'h3e44a6; end
            9'd404: begin c = 24'h0f8cfd; s = 24'h3e14fe; end
            9'd405: begin c = 24'h104fb8; s = 24'h3de2f1; end
            9'd406: begin c = 24'h1111d2; s = 24'h3dae82; end
            9'd407: begin c = 24'h11d344; s = 24'h3d77b2; end
            9'd408: begin c = 24'h129406; s = 24'h3d3e83; end
            9'd409: begin c = 24'h135411; s = 24'h3d02f7; end
            9'd410: begin c = 24'h14135d; s = 24'h3cc512; end
            9'd411: begin c = 24'h14d1e2; s = 24'h3c84d5; end
            9'd412: begin c = 24'h158f9a; s = 24'h3c4242; end
            9'd413: begin c = 24'h164c7e; s = 24'h3bfd5d; end
            9'd414: begin c = 24'h170885; s = 24'h3bb627; end
            9'd415: begin c = 24'h17c3a9; s = 24'h3b6ca5; end
            9'd416: begin c = 24'h187de3; s = 24'h3b20d8; end
            9'd417: begin c = 24'h19372a; s = 24'h3ad2c3; end
            9'd418: begin c = 24'h19ef79; s = 24'h3a826a; end
            9'd419: begin c = 24'h1aa6c8; s = 24'h3a2fcf; end
            9'd420: begin c = 24'h1b5d10; s = 24'h39daf6; end
            9'd421: begin c = 24'h1c124a; s = 24'h3983e2; end
            9'd422: begin c = 24'h1cc66f; s = 24'h392a96; end
            9'd423: begin c = 24'h1d7977; s = 24'h38cf16; end
            9'd424: begin c = 24'h1e2b5d; s = 24'h387166; end
            9'd425: begin c = 24'h1edc19; s = 24'h381188; end
            9'd426: begin c = 24'h1f8ba5; s = 24'h37af81; end
            9'd427: begin c = 24'h2039f9; s = 24'h374b55; end
            9'd428: begin c = 24'h20e70f; s = 24'h36e507; end
            9'd429: begin c = 24'h2192e1; s = 24'h367c9a; end
            9'd430: begin c = 24'h223d67; s = 24'h361215; end
            9'd431: begin c = 24'h22e69b; s = 24'h35a579; end
            9'd432: begin c = 24'h238e76; s = 24'h3536cc; end
            9'd433: begin c = 24'h2434f3; s = 24'h34c612; end
            9'd434: begin c = 24'h24da0b; s = 24'h34534f; end
            9'd435: begin c = 24'h257db6; s = 24'h33de88; end
            9'd436: begin c = 24'h261ff0; s = 24'h3367c1; end
            9'd437: begin c = 24'h26c0b1; s = 24'h32eefe; end
            9'd438: begin c = 24'h275ff4; s = 24'h327445; end
            9'd439: begin c = 24'h27fdb3; s = 24'h31f799; end
            9'd440: begin c = 24'h2899e6; s = 24'h317901; end
            9'd441: begin c = 24'h293489; s = 24'h30f880; end
            9'd442: begin c = 24'h29cd95; s = 24'h30761c; end
            9'd443: begin c = 24'h2a6505; s = 24'h2ff1da; end
            9'd444: begin c = 24'h2afad2; s = 24'h2f6bbe; end
            9'd445: begin c = 24'h2b8ef7; s = 24'h2ee3cf; end
            9'd446: begin c = 24'h2c216f; s = 24'h2e5a10; end
            9'd447: begin c = 24'h2cb232; s = 24'h2dce89; end
            9'd448: begin c = 24'h2d413d; s = 24'h2d413d; end
            9'd449: begin c = 24'h2dce89; s = 24'h2cb232; end
            9'd450: begin c = 24'h2e5a10; s = 24'h2c216f; end
            9'd451: begin c = 24'h2ee3cf; s = 24'h2b8ef7; end
            9'd452: begin c = 24'h2f6bbe; s = 24'h2afad2; end
            9'd453: begin c = 24'h2ff1da; s = 24'h2a6505; end
            9'd454: begin c = 24'h30761c; s = 24'h29cd95; end
            9'd455: begin c = 24'h30f880; s = 24'h293489; end
            9'd456: begin c = 24'h317901; s = 24'h2899e6; end
            9'd457: begin c = 24'h31f799; s = 24'h27fdb3; end
            9'd458: begin c = 24'h327445; s = 24'h275ff4; end
            9'd459: begin c = 24'h32eefe; s = 24'h26c0b1; end
            9'd460: begin c = 24'h3367c1; s = 24'h261ff0; end
            9'd461: begin c = 24'h33de88; s = 24'h257db6; end
            9'd462: begin c = 24'h34534f; s = 24'h24da0b; end
            9'd463: begin c = 24'h34c612; s = 24'h2434f3; end
            9'd464: begin c = 24'h3536cc; s = 24'h238e76; end
            9'd465: begin c = 24'h35a579; s = 24'h22e69b; end
            9'd466: begin c = 24'h361215; s = 24'h223d67; end
            9'd467: begin c = 24'h367c9a; s = 24'h2192e1; end
            9'd468: begin c = 24'h36e507; s = 24'h20e70f; end
            9'd469: begin c = 24'h374b55; s = 24'h2039f9; end
            9'd470: begin c = 24'h37af81; s = 24'h1f8ba5; end
            9'd471: begin c = 24'h381188; s = 24'h1edc19; end
            9'd472: begin c = 24'h387166; s = 24'h1e2b5d; end
            9'd473: begin c = 24'h38cf16; s = 24'h1d7977; end
            9'd474: begin c = 24'h392a96; s = 24'h1cc66f; end
            9'd475: begin c = 24'h3983e2; s = 24'h1c124a; end
            9'd476: begin c = 24'h39daf6; s = 24'h1b5d10; end
            9'd477: begin c = 24'h3a2fcf; s = 24'h1aa6c8; end
            9'd478: begin c = 24'h3a826a; s = 24'h19ef79; end
            9'd479: begin c = 24'h3ad2c3; s = 24'h19372a; end
            9'd480: begin c = 24'h3b20d8; s = 24'h187de3; end
            9'd481: begin c = 24'h3b6ca5; s = 24'h17c3a9; end
            9'd482: begin c = 24'h3bb627; s = 24'h170885; end
            9'd483: begin c = 24'h3bfd5d; s = 24'h164c7e; end
            9'd484: begin c = 24'h3c4242; s = 24'h158f9a; end
            9'd485: begin c = 24'h3c84d5; s = 24'h14d1e2; end
            9'd486: begin c = 24'h3cc512; s = 24'h14135d; end
            9'd487: begin c = 24'h3d02f7; s = 24'h135411; end
            9'd488: begin c = 24'h3d3e83; s = 24'h129406; end
            9'd489: begin c = 24'h3d77b2; s = 24'h11d344; end
            9'd490: begin c = 24'h3dae82; s = 24'h1111d2; end
            9'd491: begin c = 24'h3de2f1; s = 24'h104fb8; end
            9'd492: begin c = 24'h3e14fe; s = 24'h0f8cfd; end
            9'd493: begin c = 24'h3e44a6; s = 24'h0ec9a8; end
            9'd494: begin c = 24'h3e71e7; s = 24'h0e05c1; end
            9'd495: begin c = 24'h3e9cc0; s = 24'h0d4150; end
            9'd496: begin c = 24'h3ec530; s = 24'h0c7c5c; end
            9'd497: begin c = 24'h3eeb33; s = 24'h0bb6ed; end
            9'd498: begin c = 24'h3f0eca; s = 24'h0af10a; end
            9'd499: begin c = 24'h3f2ff2; s = 24'h0a2abb; end
            9'd500: begin c = 24'h3f4eab; s = 24'h096408; end
            9'd501: begin c = 24'h3f6af3; s = 24'h089cf8; end
            9'd502: begin c = 24'h3f84c9; s = 24'h07d594; end
            9'd503: begin c = 24'h3f9c2c; s = 24'h070de1; end
            9'd504: begin c = 24'h3fb11b; s = 24'h0645ea; end
            9'd505: begin c = 24'h3fc396; s = 24'h057db4; end
            9'd506: begin c = 24'h3fd39b; s = 24'h04b548; end
            9'd507: begin c = 24'h3fe12b; s = 24'h03ecae; end
            9'd508: begin c = 24'h3fec44; s = 24'h0323ed; end
            9'd509: begin c = 24'h3ff4e6; s = 24'h025b0d; end
            9'd510: begin c = 24'h3ffb11; s = 24'h019215; end
            9'd511: begin c = 24'h3ffec4; s = 24'h00c90f; end
            default: begin c = 24'h000000; s = 24'h000000; end
        endcase
    end
endmodule
