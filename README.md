# LINE Bot Designer & Image Recognition (Cephalopods)

> TibaMe AI班團體專題

分別串接LINE Bot、AI模型與GCP，透過團隊訓練好的模型，快速辨識台灣市場常見的五種頭足類，並蒐集用戶資料做視覺化分析。

※ 筆者負責：照片拍攝、爬蟲、資料清洗、LINE Bot程式撰寫部屬、用戶資料視覺化  
※ Github僅存放LINE Bot程式

## ◆專案目的
生活中常見的頭足類因外型相似容易混淆，也常因俗名多搞不清楚如何稱呼。
希望開發一款快速準確辨識的行動應用工具，利用AI協助使用者快速分辨台灣市場常見的頭足類名稱。

## ◆使用指南

### 加入好友  

<img src="https://drive.google.com/uc?export=view&id=1tcxdaZ76yEJVGWDEM4SzJkQEuHN0IUBK" height="350">  <img src="https://drive.google.com/uc?export=view&id=1siNDOjWCj1TWOvhIslcRTJUbMcPtZm_u" height="150">

### 圖文選單  

<img src="https://drive.google.com/uc?export=view&id=1uGcSbQMTBp9Wv6t_325V8V1wO-I4D0CJ" height="250">

### 拍照辨識  

<img src="https://drive.google.com/uc?export=view&id=1DFMgFKefaUfqxnuj_sxRYfcJDVxmfAkH" height="300">  <img src="https://drive.google.com/uc?export=view&id=1qG6XS5Ba2-hXQFstdssJPy0tZb9P_U4f" height="300">

## ◆資料蒐集及處理

### 實拍

漁港、室內  

<img src="https://drive.google.com/uc?export=view&id=1hhlT24Ap1Bo4iPOY7JgIrutRjWHSnM5T" height="250">  <img src="https://drive.google.com/uc?export=view&id=1S4xvVjzClOq7c_gZSLq6QfSjyl2Gz18t" height="250">  

### 爬蟲

圖片+關鍵字找相似圖  
<img src="https://drive.google.com/uc?export=view&id=10Yw8j-o_GQ7OcLmZ2Vyy71WHDJX9iIkv" height="250">  

以變數調整requests參數 ([完整程式](https://github.com/RabbitKM/exercise-python/blob/main/web-crawler/image-search-AdobeStock.ipynb))
```
# name: 搜尋關鍵字
# limit: 每頁幾張照片(上限100)
# page: 第幾頁
# similar_content_id: 搜尋的圖片id
# find_similar_by: 搜尋模式

params={
    'k': name,
    'limit': limit,
    'search_page': page,
    'similar_content_id': similar_content_id
    'find_similar_by': find_similar_by
}
```  
<!-- > 即時輸出爬蟲圖片
``` 
# 畫圖
def plot(ret, id_list):
    plt.figure(figsize=(15, (len(id_list)//10)*3))
    width = 10
    height = len(id_list) // width + 1
    for i in range(len(id_list)):
        url = ret['items'][id_list[i]]['content_thumb_extra_large_url']
        response = requests.get(url, stream=True)
        img = Image.open(response.raw).convert("RGB")

        plt.subplot(height, width, i+1)
        t = "{}\nW:{}\nH:{} ({})".format(id_list[i], img.width, img.height, i)
        plt.title(t)
        plt.axis("off")
        plt.imshow(img)
        
# 抓出所有圖片id
def output_all(name, serch_list):

    for dd in serch_list:
        ret, id_list = method2_image(1, dd[1], name, dd[0], dd[2])
        plot(ret, id_list) # 畫圖看一下

# 建立搜尋清單: [欲搜尋圖片id, 張數, 搜尋模式]
serch_list = [
          ['104342739', 100, 'all'],
          # ['36542254', 50, 'content'],
          # ['36542254', 50, 'color']
]

# 執行
output_all('cuttlefish', serch_list)
```  -->

### 資料清洗

排除非目標、有疑慮圖片  
<img src="https://drive.google.com/uc?export=view&id=1umHkQ5oX9xUN-DG4kKwJ-StVwvZlgtyA" height="250">  

## ◆AI模型
### 初步測試  
> 純卷積 vs 遷移學習  
> 
* CNN：從頭自行設計並輸入圖片做訓練
* VGG16：套用已被大量訓練過的參數(遷移學習)，加上自行蒐集的圖片做訓練  

※ 遷移學習效果卓越：VGG16僅做1個Epoch，超越CNN做82個Epoch的結果 (指Loss與Accuracy)

### 模型選用
> 挑選三種不同架構的Model作為Base Model
* ResNet：最常用來評比Image Recognition的模型
* Inception：在縮減參數量之下，仍然可以維持Accuracy
* EfficientNet：為了在不同平台上都能夠有效運作所設計的模型  

※ **EfficientNetB4**為最終專案模型，實際上訓練了10個模型，以4種數值作為評估標準：
* Loss：損失函數值
* Accuracy：正確率
* Time：運行時間
* Size：模型檔案大小

### 改善空間
> 無法同時分辨多種頭足類  

一張圖多種類只會抓主體特徵  
-> 做物件偵測or訓練資料要做Label處理讓模型去學  

<img src="https://drive.google.com/uc?export=view&id=1Uw6BtrOJYoM8N4_Zr-5HwTRecRGk3jp0" height="150">  

> 模型萃取特徵不正確  

背景占比太高，容易讓模型學到不必要的特徵  
-> 裁切訓練資料提高主體占比  

<img src="https://drive.google.com/uc?export=view&id=13AtRS9DUKgPqm5nmAptJM2hf2uunoykQ" height="150"> <img src="https://drive.google.com/uc?export=view&id=1cRyzMs8WTSN3aTrKsHXB2-HlkTk5Woba" height="150">


## ◆專案架構圖
使用GCP串接LINE API以及存放所有用戶資訊，另將AI模型獨立存放於Azure提高辨識效率，而Colab作為臨時AI模型伺服器可分攤流量。
<img src="https://drive.google.com/uc?export=view&id=1zwQGM_hone8c-yk8N0fT14IkAmDFHeA1" height="250">  

## ◆用戶資料分析
透過Tableau視覺化圖表，容易了解用戶操作狀態，可作為改善內容或訊息推播的參考。

* [Tableau儀表板 (動態篩選)](https://public.tableau.com/views/AI01-/1_1?:language=zh-TW&:display_count=n&:origin=viz_share_link)  
<img src="https://drive.google.com/uc?export=view&id=13p8cfNho61yI0el0ynR8i3fjtkLjzagz" height="300">  


## ◆使用套件、爬蟲圖庫、其他素材
### 套件
* flask
* gunicorn
* line-bot-sdk
* google-cloud-firestore
* google-cloud-storage
* google-cloud-logging
* aio_pika
* asyncio
* cv2
* numpy
* pandas
* sklearn
* tensorflow

### 爬蟲圖庫
* Google  
https://www.google.com.tw/imghp?hl=zh-TW
* Yandex  
https://yandex.com/images/
* Adobe Stock  
https://stock.adobe.com/photos

### 其他素材
※ 由團隊其他組員製作
* LINE插畫
* 資料清洗比較表
* AI模型資料視覺化圖
* 專案架構圖

    
<!-- ## ◆參考文件
* Mini的生活練習
https://www.facebook.com/minipractice/photos/p.1225764564231257/1225764564231257?type=3
* Google圖片爬蟲方法
https://github.com/Elwing-Chou/tibaml0606/blob/main/google_crawler.ipynb
  ※TibaMe教學。  -->

## ◆影片、簡報  
* YouTube Demo  
https://youtu.be/FeDq7sQt-xs
* YouTube 緯育成果發表  
https://youtu.be/g3_e68FYmLQ
* Google Slides  
https://docs.google.com/presentation/d/1WeIED1i0teE9zhCSkDZP7zDdYgA-ttSftLI7THzHRUc/edit?usp=sharing
