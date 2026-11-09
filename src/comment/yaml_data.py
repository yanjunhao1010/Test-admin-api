import yaml

class BaseYamlData():
    def __init__(self,filePath:str):
        self.filePath = filePath

    def read_yaml(self):

        with open(self.filePath, encoding='utf-8') as f:
            yaml_data = yaml.safe_load(f)
            return yaml_data

    def write_yaml(self, filename):
        pass