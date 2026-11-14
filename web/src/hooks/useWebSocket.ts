import { useCallback, useEffect, useRef, useState } from 'react';

type WebSocketHookResult = {
  socket: WebSocket | null;
  isConnected: boolean;
  lastMessage: any;
  sendMessage: (msg: any) => void;
};

export function useWebSocket(url: string, autoReconnect: boolean = true): WebSocketHookResult {
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<any>(null);

  // sendMessage must read the live socket. Keeping only state would leave it
  // pointing at the previous connection until a re-render happens.
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let disposed = false;
    let current: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const scheduleReconnect = () => {
      if (!autoReconnect || disposed || timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        open();
      }, 3000);
    };

    const open = () => {
      if (disposed || current) return;
      setIsConnected(false);
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch (e) {
        console.error('[WebSocket] Connection failed:', e);
        scheduleReconnect();
        return;
      }
      current = ws;
      socketRef.current = ws;
      setSocket(ws);

      ws.onopen = () => {
        if (!disposed) {
          setIsConnected(true);
          console.log(`[WebSocket] Connected to ${url}`);
        }
      };

      ws.onmessage = (event) => {
        if (disposed) return;
        try {
          setLastMessage(JSON.parse(event.data));
        } catch {
          setLastMessage(event.data);
        }
      };

      ws.onclose = () => {
        if (current === ws) current = null;
        if (socketRef.current === ws) socketRef.current = null;
        if (disposed) return;
        setIsConnected(false);
        setSocket(null);
        console.log(`[WebSocket] Disconnected from ${url}`);
        scheduleReconnect();
      };

      ws.onerror = (error) => {
        console.error('[WebSocket] Error:', error);
        // onclose handles the reconnect; closing here avoids leaving a
        // half-open socket behind when the browser does not close it itself.
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      };
    };

    open();

    return () => {
      // The old implementation closed over the *initial* `socket` state (null),
      // so unmounting never closed the live connection and the 3s reconnect
      // loop kept running forever. Everything needed for cleanup lives in this
      // effect scope instead.
      disposed = true;
      if (timer !== null) clearTimeout(timer);
      const ws = current;
      current = null;
      socketRef.current = null;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      }
    };
  }, [url, autoReconnect]);

  const sendMessage = useCallback((msg: any) => {
    const ws = socketRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(typeof msg === 'string' ? msg : JSON.stringify(msg));
    } else {
      console.warn('[WebSocket] Cannot send message, socket is not open');
    }
  }, []);

  return { socket, isConnected, lastMessage, sendMessage };
}
