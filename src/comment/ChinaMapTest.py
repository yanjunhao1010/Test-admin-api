import folium

# 创建地图
m = folium.Map(location=[35.0, 105.0], zoom_start=5)

# 添加标记点
cities = {
    "北京": [39.9042, 116.4074],
    "上海": [31.2304, 121.4737],
    "广州": [23.1291, 113.2644],
    "深圳": [22.5431, 114.0579],
    "成都": [30.5728, 104.0668]
}

for city, coords in cities.items():
    folium.Marker(
        location=coords,
        popup=f"<b>{city}</b>",
        tooltip=f"点击查看{city}",
        icon=folium.Icon(color='red', icon='info-sign')
    ).add_to(m)

# 添加线条（北京到上海）
folium.PolyLine(
    locations=[cities["北京"], cities["上海"]],
    color='blue',
    weight=2,
    opacity=0.7,
    popup="北京-上海航线"
).add_to(m)

m.save('china_markers.html')
m