extends Node

const GameDataScript = preload("uid://kb8cc1vpp45t")


func notify() -> void:
	GameData.load()
