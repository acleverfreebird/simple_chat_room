from flask import Flask, render_template, send_file

app = Flask(__name__)

@app.route('/') #从数据库中读取全部数据
def index():
    return render_template('download.html')

@app.route('/download_client') #从数据库中读取全部数据
def ans():
    return send_file('dist/cs_client.exe')
    
if __name__ == '__main__':
    app.run(debug = False, host = '0.0.0.0', port = 80)
