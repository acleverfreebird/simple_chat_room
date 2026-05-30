#######################################
### 本程序用于模拟发送get请求到服务器 ###
#######################################
import requests
import random
import time
while True:
    wd = random.randint(0, 35)  #随机生成温度值
    sd = random.randint(40, 60) #随机生成湿度值
    try:
        #向服务器IP地址发送GET请求，type表示传感器类型（温度/湿度），data表示传感器实时数据
        strurl = 'http://_________________/input?type=温度' + '&data=' + str(wd) 
        res = requests.get(url=strurl) # 使用get方式将数据发送到服务器
        print('请求发送中，','温度=' + str(wd), res.text)

        strurl = 'http://_________________/input?type=湿度' + '&data=' + str(sd) 
        res = requests.get(url=strurl)
        print('请求发送中，','湿度=' + str(sd), res.text)

    except Exception as e:
        print('无法连接到服务器，可能是您没有运行服务器端程序，请核查！！！')
    time.sleep(3)
 
