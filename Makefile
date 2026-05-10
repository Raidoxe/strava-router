UNAME_S := $(shell uname -s)

ifeq ($(UNAME_S),Darwin)
	LIB := dijkstra_core.dylib
	CC := clang
	LDFLAGS := -shared -fPIC
else
	LIB := dijkstra_core.so
	CC := gcc
	LDFLAGS := -shared -fPIC
endif

CFLAGS := -O3 -ffast-math -Wall

.PHONY: all clean run install-deps

all: $(LIB)

$(LIB): dijkstra_core.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< -lm
	@echo "Built $@"

clean:
	rm -f dijkstra_core.so dijkstra_core.dylib

# Build + install Python deps + run dev server
run: $(LIB)
	python3 -m pip install --quiet -r requirements.txt
	python3 web/server.py

install-deps:
	python3 -m pip install -r requirements.txt
